"""Lifecycle adapter; never starts a thread on module import or in Gunicorn master."""
import logging
import os
import threading

_guard=threading.Lock()
_thread=None
_stop=None

def runtime_mode(env=None):
    env=os.environ if env is None else env
    # Existing Render service uses Gunicorn. Outside Render, opt-in only.
    return env.get('BOT_POINTS_RUNTIME_MODE','embedded' if env.get('RENDER') else 'disabled').strip().lower()

def start_embedded(*, env=None, runner=None):
    global _thread,_stop
    env=os.environ if env is None else env
    if runtime_mode(env)!='embedded' or not env.get('DATABASE_URL'):
        return False
    with _guard:
        if _thread is not None and _thread.is_alive(): return False
        if runner is None:
            from render_worker import run_loop
            runner=run_loop
        _stop=threading.Event()
        _thread=threading.Thread(target=runner,args=(_stop,),kwargs={'embedded':True},name='bot-point-runtime',daemon=True)
        _thread.start()
        logging.info('BOT Point embedded runtime started')
        return True

def stop_embedded():
    # Don't block Gunicorn shutdown on Telegram/Google network calls.
    with _guard:
        if _stop is not None: _stop.set()
