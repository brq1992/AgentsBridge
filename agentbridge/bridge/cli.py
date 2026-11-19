import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import urllib.error
import urllib.request

from .store import BridgeError


# The Agent answers an ack within RemoteAgent.ACK_WAIT (20 s); the client waits longer so a
# slow Hub yields an explicit 'unconfirmed' answer instead of a lost local response.
ACK_TIMEOUT = 45
DEFAULT_TIMEOUT = 15


def call(state, route, body, timeout=DEFAULT_TIMEOUT):
    config = json.loads((Path(state) / 'client.json').read_text(encoding='utf8'))
    if not config['url'].startswith('http://127.0.0.1:'):
        raise BridgeError('M0 only connects to a loopback coordinator')
    req = urllib.request.Request(config['url'] + route,
                                 json.dumps(body, ensure_ascii=False).encode('utf8'),
                                 {'Authorization': 'Bearer ' + config['token'], 'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as response:
            data = json.load(response)
    except urllib.error.HTTPError as e:
        with e:
            try:
                data = json.load(e)
            except ValueError as cause:
                raise BridgeError(f'HTTP {e.code}: invalid/revoked client capability or rejected request') from cause
    if not data['ok']:
        raise BridgeError(data['error'])
    return data['result']


def claim(state, body, ack_timeout=ACK_TIMEOUT, receipt_attempts=3):
    """ack with an explicit receipt. Execution permission is reported only after the
    Agent has recorded that THIS caller received it; any loss before that leaves the
    claim available to the next ack, and nothing after the receipt is lost silently."""
    import secrets
    nonce = secrets.token_hex(16)
    try:
        result = call(state, '/ack', dict(body, claim_nonce=nonce), timeout=ack_timeout)
    except (OSError, TimeoutError) as e:
        return {'should_execute': False, 'reason': 'ack_response_lost_' + type(e).__name__,
                'retry': 'run the same ack again; the claim was not consumed'}
    receipt = result.pop('claim_receipt', None)
    if not receipt or not result.get('should_execute'):
        return result
    last = None
    for _ in range(receipt_attempts):
        try:
            call(state, '/ack-receipt', {'actor': body['actor'], 'operation_id': receipt, 'claim_nonce': nonce})
            return result
        except BridgeError as e:
            # A definite refusal (another caller received it, binding changed, expired).
            return {'should_execute': False, 'reason': 'claim_receipt_rejected', 'detail': str(e)[:200]}
        except (OSError, TimeoutError) as e:
            last = e
    return {'should_execute': False, 'reason': 'claim_receipt_unconfirmed_' + type(last).__name__,
            'retry': 'run the same ack again; if the receipt was recorded the claim is reported as already received'}


QUIET_KEYS = ('id', 'action', 'should_execute', 'kind', 'delivery', 'outcome', 'final', 'reason', 'error',
              'reply_delivery', 'auto_ack', 'reply_auto_ack')


def quiet(result):
    """Drop payload copies, fingerprints and generations; keep what a caller acts on."""
    if isinstance(result, dict):
        return {k: result[k] for k in QUIET_KEYS if k in result and result[k] is not None}
    if isinstance(result, list):
        return [quiet(r) for r in result]
    return result


from .policy import NOTICE_TEXT, echo_text  # noqa: E402  (JSON echo only; anything else is a notice)


def handle_message(state, body):
    """ack -> decide -> reply. Executes only what claim() authorised; replies are ack-only."""
    claimed = claim(state, dict(body))
    if not claimed.get('should_execute'):
        reason = str(claimed.get('reason') or '')
        if claimed.get('kind') in ('reply', 'notice', 'progress') and not reason:
            action = 'received'                 # ack recorded; nothing to execute
        elif reason in ('claim_already_received',):
            action = 'already_received'
        elif reason.startswith(('hub_unavailable', 'ack_response_lost', 'claim_unconfirmed',
                                'claim_receipt_unconfirmed')):
            action = 'ack_failed_retry'         # nothing was claimed; run handle again later
        else:
            action = 'not_authorised'           # expired, revoked, rebound, rejected...
        return dict(claimed, action=action)
    if claimed.get('kind') != 'request' or not claimed.get('expect_reply', True):
        return dict(claimed, action='received')
    text, outcome = echo_text(claimed.get('body'))
    reply = call(state, '/reply', dict(body, body=text, outcome=outcome, final=True))
    return dict(reply, action='echoed' if outcome == 'completed' else 'blocked_notice')


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf8')
    p = argparse.ArgumentParser(description='AgentBridge Windows M0/M1; JSON output by default')
    p.add_argument('--state', required=True)
    p.add_argument('--tool', choices=['claude', 'codex'])
    p.add_argument('--session', help='Explicit bound native UUID; Codex defaults to CODEX_THREAD_ID')
    p.add_argument('--quiet', action='store_true', help='print only id/delivery/outcome/error')
    sub = p.add_subparsers(dest='command', required=True)
    agent = sub.add_parser('agent')
    agent.add_argument('--hub-config')
    agent_start = sub.add_parser('agent-start', help='Start the M1 Agent detached unless it already runs')
    agent_start.add_argument('--hub-config')
    agent_start.add_argument('--allow-attached', action='store_true',
                             help='Start even if Windows refuses breakaway (Agent may stop with the caller)')
    agent_start.add_argument('--check-breakaway', action='store_true',
                             help='Only report whether an independent start is possible here; starts nothing')
    for name in ('doctor', 'discover', 'list', 'stop'):
        sub.add_parser(name)
    init = sub.add_parser('hub-init')
    init.add_argument('--host', action='append', required=True, help='TLS hostname or IP; repeat for each SAN')
    init.add_argument('--listen', default='127.0.0.1')
    init.add_argument('--port', type=int, default=9741)
    for name in ('hub','hub-status','hub-stop'):
        sub.add_parser(name)
    add = sub.add_parser('hub-node-add')
    add.add_argument('--label', required=True)
    add.add_argument('--project', action='append', required=True)
    add.add_argument('--url', required=True)
    add.add_argument('--local-alias', action='append')
    revoke = sub.add_parser('hub-node-revoke')
    revoke.add_argument('--node', required=True)
    invite = sub.add_parser('hub-invite')
    invite.add_argument('--node', required=True, help='Node UUID')
    invite.add_argument('--remote-ip', required=True)
    enroll = sub.add_parser('node-enroll')
    enroll.add_argument('--url', required=True)
    enroll.add_argument('--certificate', required=True)
    enroll.add_argument('--fingerprint', required=True)
    enroll.add_argument('--code', required=True)
    join = sub.add_parser('node-import')
    join.add_argument('--file', required=True)
    bind = sub.add_parser('bind')
    bind.add_argument('--alias', required=True)
    bind.add_argument('--project', required=True)
    bind.add_argument('--cli', default=shutil.which('codex'))
    bind.add_argument('--codex-home', default=os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    bind_self = sub.add_parser('bind-self', help='Bind the calling conversation itself (identity detected) and grant its client')
    bind_self.add_argument('--alias', required=True)
    bind_self.add_argument('--project', help='Default: site.json "project", else agentbridge-m0')
    bind_self.add_argument('--client-directory', help='Default: <state parent>/clients/<alias>/agentbridge-client')
    unbind = sub.add_parser('unbind')
    unbind.add_argument('--alias', required=True)
    refresh = sub.add_parser('refresh-claude')
    refresh.add_argument('--alias', required=True)
    refresh_codex = sub.add_parser('refresh-codex', help='Same-generation Codex CLI path renewal after an app update')
    refresh_codex.add_argument('--alias', required=True)
    refresh_codex.add_argument('--cli', help='New codex.exe; omitted = the single installed sibling version')
    qualify = sub.add_parser('qualify-claude', help='Approve a Claude version after a live structural check')
    qualify.add_argument('--version', required=True)
    grant = sub.add_parser('grant-client')
    grant.add_argument('--alias', required=True)
    grant.add_argument('--directory', required=True)
    grant.add_argument('--reader-sid')
    send = sub.add_parser('send')
    send.add_argument('--to', required=True)
    send.add_argument('--id')
    send.add_argument('--ttl', type=float, default=86400)
    reply = sub.add_parser('reply')
    reply.add_argument('--message', required=True)
    reply.add_argument('--final', action='store_true', required=True)
    reply.add_argument('--outcome', choices=['completed', 'blocked'], default='completed')
    for parser in (send, reply):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--text')
        group.add_argument('--body-file')
    handle = sub.add_parser('handle', help='ack, then apply the echo-only policy and reply in one step')
    handle.add_argument('--message', required=True)
    ack = sub.add_parser('ack')
    ack.add_argument('--message', required=True)
    status = sub.add_parser('status')
    status.add_argument('--message')
    args = p.parse_args()
    try:
        if args.command.startswith('hub') or args.command in ('node-import','node-enroll'):
            from .provision import administer
            result = administer(args)
            if result is not None:
                print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
            return
        if args.command == 'agent':
            from .server import run
            run(args.state, args.hub_config)
            return
        if args.command == 'agent-start':
            from .spawn import agent_start, breakaway_possible
            if args.check_breakaway:
                result = {'independent_start_possible': breakaway_possible()}
            else:
                result = agent_start(args.state, args.hub_config, allow_attached=args.allow_attached)
            print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
            return
        if args.command == 'bind-self':
            from .selfbind import bind_self
            if args.tool or args.session:
                raise BridgeError('bind-self detects the calling conversation; use bind for any other session')
            result = bind_self(args.state, args.alias, args.project, args.client_directory)
            print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
            return
        tool = args.tool or ('codex' if os.environ.get('CODEX_THREAD_ID') else None)
        native = args.session or (os.environ.get('CODEX_THREAD_ID') if tool == 'codex' else None)
        if args.command in ('send', 'ack', 'reply', 'bind', 'handle') and (not tool or not native):
            raise BridgeError('native tool/session identity is required')
        context = {'codex_thread_id': os.environ.get('CODEX_THREAD_ID'), 'cli_pid': os.getpid()}
        if tool == 'codex' and args.command in ('send', 'ack', 'reply', 'handle') and context['codex_thread_id'] and context['codex_thread_id'] != native:
            raise BridgeError('explicit Codex session differs from calling CODEX_THREAD_ID')
        body = {'actor': f'{tool}:{native}', 'context': context}
        routes = {'doctor': '/health', 'discover': '/discover', 'list': '/sessions', 'unbind': '/unbind',
                  'stop': '/stop', 'bind': '/bind', 'send': '/send', 'ack': '/ack', 'reply': '/reply', 'status': '/status',
                  'grant-client': '/grant', 'refresh-claude': '/refresh-claude',
                  'refresh-codex': '/refresh-codex', 'qualify-claude': '/qualify-claude'}
        if args.command == 'bind':
            body.update(tool=tool, native_id=native, alias=args.alias, project=args.project,
                        cli=args.cli, codex_home=args.codex_home)
        if args.command in ('unbind', 'refresh-claude', 'refresh-codex'):
            body['identity'] = args.alias
        if args.command == 'refresh-codex':
            body['cli'] = args.cli
        if args.command == 'qualify-claude':
            body['version'] = args.version
        if args.command == 'grant-client':
            body.update(identity=args.alias, directory=args.directory, reader_sid=args.reader_sid)
        if args.command in ('send', 'reply'):
            body['body'] = Path(args.body_file).read_text(encoding='utf-8-sig') if args.body_file else args.text
        if args.command == 'send':
            body.update(to=args.to, message_id=args.id, ttl=args.ttl)
        if args.command in ('ack', 'reply', 'status', 'handle'):
            body['message_id'] = args.message
        if args.command == 'reply':
            body['outcome'] = args.outcome
        if args.command == 'handle':
            result = handle_message(args.state, body)
        elif args.command == 'ack':
            result = claim(args.state, body)
        else:
            result = call(args.state, routes[args.command], body)
        if args.quiet:
            result = quiet(result)
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
    except (BridgeError, OSError, ValueError) as e:
        print(json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
        sys.exit(1)
