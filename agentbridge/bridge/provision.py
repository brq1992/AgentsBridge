"""Local administration. Secrets are written only to a protected state directory."""
import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess

from .store import BridgeError


def private_directory(path):
    path = Path(path).resolve()
    if path == Path(path.anchor) or path == Path.home() or path == Path.cwd():
        raise BridgeError('choose a dedicated private state directory')
    path.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        sid = list(csv.reader(subprocess.check_output(['whoami','/user','/fo','csv'],text=True).splitlines()))[-1][1]
        api = ctypes.WinDLL('advapi32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        descriptor = ctypes.c_void_p()
        api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,ctypes.POINTER(ctypes.c_void_p),ctypes.c_void_p]
        api.SetFileSecurityW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,ctypes.c_void_p]
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        sddl = f'D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)'
        if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,ctypes.byref(descriptor),None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not api.SetFileSecurityW(str(path),0x80000004,descriptor):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.LocalFree(descriptor)
    else:
        path.chmod(0o700)
    return path


def certificate_expiry(pem, now=None):
    """Expiry of the pinned Hub certificate; there is no automatic rotation, so surface it."""
    import datetime
    from cryptography import x509
    certificate = x509.load_pem_x509_certificate(pem.encode('ascii') if isinstance(pem, str) else pem)
    not_after = certificate.not_valid_after_utc
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return {'not_after': not_after.isoformat(), 'days_left': (not_after - now).days}


def save_private(path, data):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')
    if os.name != 'nt':
        temporary.chmod(0o600)
    temporary.replace(path)


def administer(args):
    from .hub import HubStore, create_tls, serve_hub
    state = private_directory(args.state)
    if args.command == 'node-enroll':
        import ssl
        from websockets.sync.client import connect
        from .network import local_identity
        certificate=Path(args.certificate).read_text(encoding='ascii')
        fingerprint=hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate)).hexdigest()
        if fingerprint.lower()!=args.fingerprint.lower() or not args.url.startswith('wss://'):
            raise BridgeError('certificate fingerprint mismatch or insecure Hub URL')
        target=state/'network.json'
        if target.exists():
            raise BridgeError('node already configured; refusing to overwrite credentials')
        identity=local_identity(state)
        context=ssl.create_default_context(cadata=certificate)
        with connect(args.url,ssl=context,proxy=None,open_timeout=8) as ws:
            ws.send(json.dumps({'op':'enroll','code':args.code,**identity}))
            response=json.loads(ws.recv(timeout=8))
            if not response.get('ok'):
                raise BridgeError('enrollment rejected')
        data=response['enrollment']|{'hub_url':args.url,'hub_certificate':certificate}
        save_private(target,data)
        return {'enrolled':True,'node':data['label'],'state':str(state),'certificate_sha256':fingerprint}
    if args.command == 'hub-init':
        config = state/'hub.json'
        if config.exists():
            raise BridgeError('Hub already initialized')
        fingerprint = create_tls(state,args.host)
        save_private(config,{'listen':args.listen,'port':args.port,'certificate_sha256':fingerprint})
        HubStore(state/'hub.sqlite')
        return {'initialized':str(state),'certificate_sha256':fingerprint,'listen':args.listen,'port':args.port}
    if args.command == 'node-import':
        data = json.loads(Path(args.file).read_text(encoding='utf8'))
        required = ('node_id','token','label','projects','hub_url','hub_certificate')
        if any(not data.get(key) for key in required) or not data['hub_url'].startswith('wss://'):
            raise BridgeError('invalid enrollment file')
        target = state/'network.json'
        if target.exists() and json.loads(target.read_text(encoding='utf8')) != data:
            raise BridgeError('existing node configuration differs; explicit migration required')
        save_private(target,data)
        return {'imported':str(target),'node':data['label'],'hub_url':data['hub_url']}
    if not (state/'hub.json').exists():
        raise BridgeError('initialize Hub first')
    config = json.loads((state/'hub.json').read_text(encoding='utf8'))
    store = HubStore(state/'hub.sqlite')
    if args.command == 'hub-invite':
        code=store.invite(args.node,args.remote_ip)
        path=state/'invitation.json'
        save_private(path,{'code':code,'node_id':args.node,'remote_ip':args.remote_ip})
        return {'invitation_file':str(path),'expires_in_seconds':3600,'source_ip':args.remote_ip}
    if args.command == 'hub':
        import threading
        stop_file=state/'hub.stop'
        stop_file.unlink(missing_ok=True)
        finished=threading.Event()
        def ready(server):
            save_private(state/'hub-process.json',{'pid':os.getpid()})
            def monitor():
                while not finished.wait(0.3):
                    if stop_file.exists():
                        server.shutdown()
                        return
            threading.Thread(target=monitor,daemon=True).start()
        try:
            serve_hub(store,config['listen'],config['port'],state/'hub-cert.pem',state/'hub-key.pem',ready)
        finally:
            finished.set()
        return None
    if args.command == 'hub-stop':
        (state/'hub.stop').write_text('stop',encoding='ascii')
        return {'stopping':True}
    if args.command == 'hub-status':
        overview = store.overview()
        overview['certificate'] = certificate_expiry((state/'hub-cert.pem').read_text(encoding='ascii'))
        return overview
    if args.command == 'hub-node-revoke':
        return store.revoke(args.node)
    if args.command == 'hub-node-add':
        if not args.url.startswith('wss://'):
            raise BridgeError('Hub URL must use wss://')
        directory = private_directory(state/'enrollment')
        path = directory/(args.label+'.json')
        # Validate node label in add_node before using it as a filename.
        data = store.add_node(args.label,args.project)
        data.update(hub_url=args.url,hub_certificate=(state/'hub-cert.pem').read_text(encoding='ascii'))
        if args.local_alias:
            data['local_aliases'] = args.local_alias
        save_private(path,data)
        return {'node_id':data['node_id'],'label':data['label'],'enrollment_file':str(path),
                'certificate_sha256':config['certificate_sha256']}
    raise BridgeError('unknown administration command')
