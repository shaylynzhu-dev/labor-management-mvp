from flask import jsonify


def api_response(code=0, message="ok", data=None, http_status=200):
    return jsonify({"code": code, "message": message, "data": data}), http_status
