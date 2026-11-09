"""Safe deployment entrypoint until Bot Points application sources are committed."""
import os
from flask import Flask, jsonify
from gateway_auth import install_gateway_auth

app = Flask(__name__)
install_gateway_auth(app)

@app.get("/")
def maintenance():
    return jsonify(status="deployment_pending", message="Bot Points integration not enabled"), 503
