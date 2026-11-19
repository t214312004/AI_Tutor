"""Isolated PreInvocation hook. The app, rather than the model, owns this bridge."""

import json
import socket
import sys


def main():
    config, = sys.argv[1:]
    with open(config, encoding='utf-8') as source:
        auth = json.load(source)
    event = json.loads(sys.stdin.buffer.read(262_145))
    if not isinstance(event, dict):
        return {}
    request = {'token': auth['token'], 'conversation': event.get('conversationId'),
               'invocation': event.get('invocationNum')}
    with socket.create_connection(('127.0.0.1', auth['port']), timeout=2) as connection:
        connection.sendall(json.dumps(request).encode() + b'\n')
        with connection.makefile('rb') as source:
            answer = json.loads(source.readline(1_048_576))
    if isinstance(answer, str) and answer:
        return {'injectSteps': [{'ephemeralMessage': answer}]}
    return {}


if __name__ == '__main__':
    try:
        result = main()
    except (OSError, ValueError, TypeError, KeyError):
        result = {}
    print(json.dumps(result, ensure_ascii=True))
