"""Independent web login and signed StaffFlow gateway, fail closed."""
import hashlib
import hmac
import os
import re
import secrets
import time
from flask import g, jsonify, request, session, redirect, render_template_string
from werkzeug.security import check_password_hash

WINDOW = 60
SESSION_TTL = 8 * 3600
SSO_SESSION_TTL = 3600
SSO_TICKET_TTL = 60
LOGIN_PAGE = """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BOT Point — вход</title>
<style>body{background:#111827;color:#e5e7eb;font:16px system-ui;display:grid;place-items:center;min-height:95vh;margin:0}main{width:min(340px,86vw);padding:28px;background:#1f2937;border:1px solid #374151;border-radius:16px}h1{margin-top:0}input,button{box-sizing:border-box;width:100%;padding:12px;margin:10px 0;border:1px solid #475569;border-radius:8px;font:inherit}input{background:#111827;color:#fff}button{background:#2563eb;color:white;cursor:pointer}p{color:#a5b4c7}</style>
<main><h1>BOT Point</h1><p>Независимый вход администратора</p>
{% if error %}<p role="alert">{{ error }}</p>{% endif %}
<form method="post" action="/login"><input type="hidden" name="csrf" value="{{ csrf }}">
<label>Пароль<input type="password" name="password" autocomplete="current-password" required maxlength="256"></label>
<button type="submit">Войти</button></form></main></html>"""

def signature(secret, method, path, query, timestamp, nonce, body):
    digest = hashlib.sha256(body).hexdigest()
    msg = '\n'.join((method.upper(), path, query, timestamp, nonce, digest))
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

def _password_hash():
    return os.environ.get('BOT_POINTS_ADMIN_PASSWORD_HASH', '').strip()

def _password_config():
    hashed=_password_hash()
    if hashed:
        return 'hash:'+hashed
    password=os.environ.get('BOT_POINTS_ADMIN_PASSWORD','')
    return 'password:'+password if 16<=len(password)<=256 else ''

def _password_matches(password):
    hashed=_password_hash()
    if not 0<len(password)<=256:
        return False
    if hashed:
        return check_password_hash(hashed,password)
    configured=os.environ.get('BOT_POINTS_ADMIN_PASSWORD','')
    return 16<=len(configured)<=256 and hmac.compare_digest(
        hashlib.sha256(configured.encode()).digest(),hashlib.sha256(password.encode()).digest())

def _session_marker():
    return hashlib.sha256(_password_config().encode()).hexdigest()

def _independent_session():
    return bool(_password_config() and session.get('points_admin') == _session_marker()
                and isinstance(session.get('points_until'),int)
                and session['points_until'] > int(time.time()))

def _sso_marker():
    return hashlib.sha256(os.environ.get('STAFFFLOW_POINTS_GATEWAY_SECRET','').encode()).hexdigest()

def _sso_session():
    return bool(len(os.environ.get('STAFFFLOW_POINTS_GATEWAY_SECRET',''))>=32
                and session.get('points_sso')==_sso_marker()
                and isinstance(session.get('points_until'),int)
                and session['points_until']>int(time.time()))

def _valid_sso_ticket(ticket):
    secret=os.environ.get('STAFFFLOW_POINTS_GATEWAY_SECRET','')
    if len(secret)<32 or not isinstance(ticket,str) or len(ticket)>200:
        return False
    match=re.fullmatch(r'v1\.(\d{1,12})\.([0-9a-f]{64})\.([0-9a-f]{64})',ticket)
    if not match:
        return False
    expiry,nonce,sig=match.groups()
    now=int(time.time())
    expiry_int=int(expiry)
    if not now<=expiry_int<=now+SSO_TICKET_TTL:
        return False
    expected=hmac.new(secret.encode(),f'bot-point-sso:v1:{expiry_int}:{nonce}'.encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig,expected):
        return False
    from neon_store import connect
    with connect() as db:
        db.execute('DELETE FROM gateway_nonces WHERE expires_at < %s',(now,))
        row=db.execute('INSERT INTO gateway_nonces(nonce,expires_at) VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING nonce',
                       ('sso:'+nonce,expiry_int)).fetchone()
        return row is not None

SSO_PAGE = """<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BOT Point — подключение</title><style>body{background:#111827;color:#e5e7eb;font:16px system-ui;padding:32px}a{color:#93c5fd}</style>
<h1>BOT Point</h1><p id="notice">Подключение к независимому приложению…</p>
<a href="/login">Самостоятельный вход</a>
<script>
(async()=>{
 const ticket=new URLSearchParams(location.hash.slice(1)).get('ticket')||'';
 history.replaceState(null,'',location.pathname);
 try{
  const r=await fetch('/sso',{method:'POST',headers:{'Content-Type':'application/json','X-Bot-Points-Request':'1'},body:JSON.stringify({ticket})});
  if(!r.ok)throw Error('Билет истёк или недействителен. Откройте BOT Point заново из StaffFlow.');
  location.replace('/');
 }catch(e){document.getElementById('notice').textContent=e.message}
})();
</script></html>"""

def _login_allowed():
    # Shared DB throttle, including after process restart; no untrusted IP header.
    from neon_store import connect
    bucket=int(time.time())//300
    with connect() as db:
        db.execute('DELETE FROM points_login_attempts WHERE bucket < %s',(bucket-1,))
        row=db.execute('''INSERT INTO points_login_attempts(bucket,attempts)
            VALUES (%s,1) ON CONFLICT(bucket) DO UPDATE SET
            attempts=points_login_attempts.attempts+1 RETURNING attempts''',(bucket,)).fetchone()
    return row['attempts']<=12

def install_gateway_auth(app):
    app.config.update(SESSION_COOKIE_NAME='bot_points_admin',
                      SESSION_COOKIE_SECURE=bool(os.environ.get('RENDER')),
                      SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Strict',
                      MAX_CONTENT_LENGTH=2*1024*1024)

    @app.route('/sso',methods=['GET','POST'])
    def points_sso():
        if not os.environ.get('FLASK_SECRET_KEY'):
            return 'Вход BOT Point ещё не настроен.',503
        if request.method=='GET':
            return SSO_PAGE
        if request.headers.get('X-Bot-Points-Request')!='1':
            return jsonify(error='Forbidden'),403
        data=request.get_json(silent=True) or {}
        if not isinstance(data,dict):
            return jsonify(error='Invalid ticket'),400
        try:
            if not _valid_sso_ticket(data.get('ticket')):
                return jsonify(error='Invalid or expired ticket'),401
        except Exception:
            return jsonify(error='SSO unavailable'),503
        session.clear()
        session['points_sso']=_sso_marker()
        session['points_until']=int(time.time())+SSO_SESSION_TTL
        return jsonify(ok=True)

    @app.route('/login', methods=['GET','POST'])
    def points_login():
        if not _password_config() or not os.environ.get('FLASK_SECRET_KEY'):
            return 'Самостоятельный вход BOT Point ещё не настроен.',503
        if request.method=='GET':
            if _independent_session() or _sso_session():
                return redirect('/')
            session['points_csrf']=secrets.token_urlsafe(32)
            return render_template_string(LOGIN_PAGE,csrf=session['points_csrf'],error='')
        token=session.get('points_csrf','')
        offered=request.form.get('csrf','')
        if not token or not hmac.compare_digest(token,offered):
            return 'Обновите страницу входа.',403
        try:
            permitted=_login_allowed()
        except Exception:
            return 'Сервис входа временно недоступен.',503
        if not permitted:
            return 'Слишком много попыток. Повторите через несколько минут.',429
        password=request.form.get('password','')
        try:
            valid=_password_matches(password)
        except (TypeError,ValueError):
            valid=False
        if not valid:
            return render_template_string(LOGIN_PAGE,csrf=token,error='Неверный пароль.'),401
        session.clear()
        session['points_admin']=_session_marker()
        session['points_until']=int(time.time())+SESSION_TTL
        return redirect('/')

    @app.route('/logout',methods=['POST'])
    def points_logout():
        if request.headers.get('X-Bot-Points-Request')!='1':
            return jsonify(error='Forbidden'),403
        session.clear()
        return jsonify(ok=True)

    @app.before_request
    def verify():
        if request.path == '/healthz' and request.method == 'GET':
            return None
        if request.path in ('/login','/sso') and request.method in ('GET','POST'):
            return None
        timestamp = request.headers.get('X-Points-Timestamp', '')
        nonce = request.headers.get('X-Points-Nonce', '')
        signed = request.headers.get('X-Points-Signature', '')
        if not (timestamp or nonce or signed):
            if _independent_session() or _sso_session():
                if request.method not in ('GET','HEAD') and request.headers.get('X-Bot-Points-Request')!='1':
                    return jsonify(error='Forbidden'),403
                g.points_authenticated=True
                return None
            if request.path=='/' and request.method=='GET' and _password_config():
                return redirect('/login')
            if not any(len(os.environ.get(k,''))>=32 for k in
                       ('BOT_POINTS_GATEWAY_SECRET','STAFFFLOW_POINTS_GATEWAY_SECRET')) and not _password_config():
                return jsonify(error='Gateway not configured'),503
            return jsonify(error='Unauthorized'),401
        keys=[os.environ.get(k,'') for k in
              ('STAFFFLOW_POINTS_GATEWAY_SECRET','BOT_POINTS_GATEWAY_SECRET')]
        keys=[k for k in keys if len(k)>=32]
        if not keys:
            return jsonify(error='Gateway not configured'),503
        if not timestamp.isdecimal() or len(timestamp)>15 or not 16<=len(nonce)<=128 or len(signed)!=64:
            return jsonify(error='Unauthorized'),401
        now=int(time.time())
        if abs(now-int(timestamp))>WINDOW:
            return jsonify(error='Unauthorized'),401
        if not any(hmac.compare_digest(signature(key,request.method,request.path,
                request.query_string.decode('latin-1'),timestamp,nonce,request.get_data(cache=True)),signed) for key in keys):
            return jsonify(error='Unauthorized'),401
        if not os.environ.get('DATABASE_URL'):
            return jsonify(error='Replay protection unavailable'),503
        try:
            from neon_store import connect
            with connect() as db:
                db.execute('DELETE FROM gateway_nonces WHERE expires_at < %s',(now,))
                row=db.execute('INSERT INTO gateway_nonces(nonce,expires_at) VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING nonce',
                               (nonce,int(timestamp)+WINDOW)).fetchone()
                if row is None:
                    return jsonify(error='Replay rejected'),401
        except Exception:
            return jsonify(error='Gateway unavailable'),503
        g.points_authenticated=True
        return None

    @app.after_request
    def security_headers(response):
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

def sign_gateway_request(secret, method, path, query, body, *, nonce, timestamp):
    return {'X-Points-Timestamp': str(timestamp), 'X-Points-Nonce': nonce,
            'X-Points-Signature': signature(secret, method, path, query, str(timestamp), nonce, body)}
