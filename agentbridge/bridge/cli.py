import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import urllib.error
import urllib.request

from .store import BridgeError


def call(state, route, body):
    config = json.loads((Path(state) / 'client.json').read_text(encoding='utf8'))
    if not config['url'].startswith('http://127.0.0.1:'):
        raise BridgeError('M0 only connects to a loopback coordinator')
    req = urllib.request.Request(config['url'] + route,
                                 json.dumps(body, ensure_ascii=False).encode('utf8'),
                                 {'Authorization': 'Bearer ' + config['token'], 'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=15) as response:
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


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf8')
    p = argparse.ArgumentParser(description='AgentBridge Windows M0/M1; JSON output by default')
    p.add_argument('--state', required=True)
    p.add_argument('--tool', choices=['claude', 'codex'])
    p.add_argument('--session', help='Explicit bound native UUID; Codex defaults to CODEX_THREAD_ID')
    sub = p.add_subparsers(dest='command', required=True)
    agent = sub.add_parser('agent')
    agent.add_argument('--hub-config')
    agent_start = sub.add_parser('agent-start', help='Start the M1 Agent detached unless it already runs')
    agent_start.add_argument('--hub-config')
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
            from .spawn import agent_start
            print(json.dumps({'ok': True, 'result': agent_start(args.state, args.hub_config)}, ensure_ascii=False))
            return
        tool = args.tool or ('codex' if os.environ.get('CODEX_THREAD_ID') else None)
        native = args.session or (os.environ.get('CODEX_THREAD_ID') if tool == 'codex' else None)
        if args.command in ('send', 'ack', 'reply', 'bind') and (not tool or not native):
            raise BridgeError('native tool/session identity is required')
        context = {'codex_thread_id': os.environ.get('CODEX_THREAD_ID'), 'cli_pid': os.getpid()}
        if tool == 'codex' and args.command in ('send', 'ack', 'reply') and context['codex_thread_id'] and context['codex_thread_id'] != native:
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
        if args.command in ('ack', 'reply', 'status'):
            body['message_id'] = args.message
        if args.command == 'reply':
            body['outcome'] = args.outcome
        result = call(args.state, routes[args.command], body)
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
    except (BridgeError, OSError, ValueError) as e:
        print(json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
        sys.exit(1)
