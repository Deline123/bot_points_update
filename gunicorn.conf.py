"""Automatically loaded by the existing Render Gunicorn start command."""
def post_worker_init(worker):
    from embedded_runtime import start_embedded
    start_embedded()

def worker_exit(server, worker):
    from embedded_runtime import stop_embedded
    stop_embedded()
