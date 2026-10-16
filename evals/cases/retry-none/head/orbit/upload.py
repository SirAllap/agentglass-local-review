import time


def upload(send, data, retries=3):
    """Send data, trying again when send raises OSError.

    `retries` is how many times to try in all."""
    last = None
    for attempt in range(retries):
        try:
            return send(data)
        except OSError as e:
            last = e
            time.sleep(2 ** attempt)
    raise last
