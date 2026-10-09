"""Request signing shared by the master's internal API and the workers."""
import hashlib
import hmac


def sign(secret, method, path, timestamp, body):
    message = "\n".join([method.upper(), path, str(timestamp), hashlib.sha256(body).hexdigest()]).encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()
