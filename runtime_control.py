"""Shared runtime state and cross-process operation locks (Postgres or local SQLite)."""
import json
import os
import threading
import time
import uuid
from datetime import datetime, time as day_time
from zoneinfo import ZoneInfo

def init_runtime(c):
    c.execute("CREATE TABLE IF NOT EXISTS report_ingest_versions(chat_id BIGINT NOT NULL,message_id BIGINT NOT NULL,event_utc BIGINT NOT NULL,PRIMARY KEY(chat_id,message_id))")
    c.execute("CREATE TABLE IF NOT EXISTS runtime_events(id TEXT PRIMARY KEY, component TEXT NOT NULL, state TEXT NOT NULL, at BIGINT NOT NULL, error TEXT)")
    c.execute("CREATE INDEX IF NOT EXISTS runtime_events_at ON runtime_events(at)")

class SharedLock:
    """Transaction-scoped advisory lock works with Neon transaction poolers.
    Keep a dedicated DB transaction open for the operation; local uses a thread lock.
    """
    def __init__(self, connect, key):
        self.connect=connect
        self.key=key
        self.local=threading.Lock()
        self.db=None

    def acquire(self, blocking=True):
        if not self.local.acquire(blocking=blocking): return False
        try:
            if not os.getenv('DATABASE_URL'): return True
            db=self.connect()
            try:
                db.execute("SET LOCAL idle_in_transaction_session_timeout = '0'")
                if blocking:
                    db.execute("SELECT pg_advisory_xact_lock(?)",(self.key,))
                elif not db.execute("SELECT pg_try_advisory_xact_lock(?) AS acquired",(self.key,)).fetchone()['acquired']:
                    db.close()
                    self.local.release()
                    return False
            except BaseException:
                db.close()
                raise
            self.db=db
            return True
        except BaseException:
            self.local.release()
            raise

    def release(self):
        try:
            if self.db is not None: self.db.close()
        finally:
            self.db=None
            self.local.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self,*args):
        self.release()

def read_status(connect, component):
    with connect() as c:
        row=c.execute("SELECT value FROM meta WHERE key=?",('runtime:'+component,)).fetchone()
    return json.loads(row['value']) if row else {}

def record_status(connect, component, state, *, error=None, details=None, now=None):
    now=int(time.time() if now is None else now)
    with connect() as c:
        key='runtime:'+component
        row=c.execute("SELECT value FROM meta WHERE key=?",(key,)).fetchone()
        old=json.loads(row['value']) if row else {}
        record={**old,'state':state,'at':now,'error':error}
        if details is not None:
            record.update(details=details)
            for k in ('last_update','bot_name'):
                if details.get(k): record[k]=details[k]
        if state=='ok': record['last_success']=now
        c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,json.dumps(record,ensure_ascii=False)))
        if state=='error' or old.get('state')!=state or component=='history':
            c.execute("INSERT INTO runtime_events(id,component,state,at,error) VALUES(?,?,?,?,?)",(uuid.uuid4().hex,component,state,now,error))
        c.execute("DELETE FROM runtime_events WHERE at<?",(now-30*86400,))
    return record

def dashboard(connect, chat, topic, env, auto, google_configured, history, *, now=None):
    now=int(time.time() if now is None else now)
    kyiv=ZoneInfo('Europe/Kyiv')
    day=datetime.fromtimestamp(now,kyiv).date()
    since=int(datetime.combine(day,day_time.min,kyiv).timestamp())
    scope=''; args=[]
    if chat: scope+=' AND chat_id=?'; args.append(int(chat))
    if topic: scope+=' AND thread_id=?'; args.append(int(topic))
    def count(c,sql,params=()):
        return int(c.execute(sql,params).fetchone()['n'])
    with connect() as c:
        processed=count(c,'SELECT COUNT(*) AS n FROM messages WHERE date_utc>=? AND date_utc<=?'+scope,(since,now,*args))
        review=count(c,'SELECT COUNT(*) AS n FROM review_messages WHERE 1=1'+scope,args)
        if auto.get('sid') and auto.get('gid') is not None:
            review+=count(c,"SELECT COUNT(*) AS n FROM sheet_point_reviews WHERE status='pending' AND spreadsheet_id=? AND gid=?",(auto['sid'],str(auto['gid'])))
            parts=count(c,'SELECT COUNT(*) AS n FROM sheet_applied WHERE updated_utc>=? AND spreadsheet_id=? AND gid=?',(since,auto['sid'],str(auto['gid'])))
        else: parts=0
        errors=count(c,"SELECT COUNT(*) AS n FROM runtime_events WHERE state='error' AND at>=?",(since,))
        events=[dict(r) for r in c.execute("SELECT component,state,at,error FROM runtime_events ORDER BY at DESC,id DESC LIMIT 12").fetchall()]
    records={k:read_status(connect,k) for k in ('worker','telegram','history','google')}
    def state(k,configured=True,ttl=360):
        r=records[k]
        if not configured:return 'not_configured'
        if not r:return 'waiting'
        if now-r.get('at',0)>ttl:return 'stale'
        return r.get('state','waiting')
    worker=state('worker') if env.get('DATABASE_URL') else 'local'
    writes=env.get('BOT_POINTS_WRITES_ENABLED')=='1' and (not env.get('DATABASE_URL') or env.get('BOT_POINTS_CLOUD_WRITES_CONFIRMED')=='1')
    tel_config=bool(env.get('BOT_TOKEN') and chat and topic)
    hist_config=bool(env.get('API_ID') and env.get('API_HASH') and env.get('TELETHON_STRING_SESSION') and chat and topic)
    return {'version':'4.1.0','counts':{'processed':processed,'review':review,'parts':parts,'errors':errors},
            'services':{'worker':worker,'telegram':state('telegram',tel_config),'history':state('history',hist_config,3600),
                        'google':state('google',google_configured,360) if writes and auto.get('enabled') else ('disabled' if google_configured else 'not_configured'),
                        'database':'ok','staffflow':'configured' if len(env.get('STAFFFLOW_POINTS_GATEWAY_SECRET',''))>=32 else 'not_configured'},
            'writes_enabled':writes,'auto_enabled':bool(auto.get('enabled')),'history_last_success':history.get('last_success'),'records':records,'events':events}
