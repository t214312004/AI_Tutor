"""Independent speaker streams, with provisional boundaries rather than invented IDs."""
from datetime import datetime
import time
from uuid import uuid4

PROJECTION_VERSION = 1


class TranscriptProjection:
    def __init__(self, gap_ms=3500):
        self.gap_ms = gap_ms
        self.current = {}

    def append(self, role, delta, start_ms=None, end_ms=None, *, received=None,
               ident=None, finished=False, delivery='provider_transcript_not_playback_receipt'):
        if role not in ('user', 'assistant') or not isinstance(delta, str):
            raise ValueError('Invalid transcript fragment')
        received = time.monotonic() if received is None else received
        item = self.current.get(role)
        gap = (start_ms - item['end_ms'] if item and start_ms is not None
               and item.get('end_ms') is not None else
               (received - item['received']) * 1000 if item else 0)
        if item is None or item.get('complete') or gap > self.gap_ms or ident and ident != item['id'] or item and len(item['text'])+len(delta)>8000:
            item = {'id': ident or uuid4().hex, 'role': role, 'text': '', 'start_ms': start_ms,
                    'end_ms': end_ms, 'complete': False, 'status': delivery,
                    'projection_version': PROJECTION_VERSION}
            self.current[role] = item
        item['text'] += delta  # Spaces, repetitions and punctuation are source evidence.
        item['received'] = received
        if delivery != item.get('status'):
            item['status'] = delivery
        if end_ms is not None:
            item['end_ms'] = end_ms
        if finished:
            item['complete'] = True
        return item

    def finish(self, role):
        if role in self.current:
            self.current[role]['complete'] = True


def event_time(event, fallback):
    try:
        return datetime.fromisoformat(event['created'].replace('Z', '+00:00')).timestamp()
    except (KeyError, ValueError, TypeError):
        return fallback


def projected_records(events):
    """Rebuild legacy fragments without overwriting source events or claiming playback."""
    projection = TranscriptProjection()
    result = []
    rows = {}
    for index, event in enumerate(events):
        if event['kind'] == 'speaker_context':
            projection.finish('user')
            continue
        if event['kind'] != 'live_transcript':
            continue
        p = event['payload']
        role = p.get('role')
        if role not in ('user', 'assistant') or not p.get('text'):
            continue
        speaker=p.get('speaker','unknown' if role=='user' else 'teacher')
        if role in rows and rows[role][1]['data']['speaker'] != speaker:
            projection.finish(role)
        item = projection.append(role, p['text'], p.get('start_ms'), p.get('end_ms'),
            received=event_time(event, index / 1000),
            ident=p.get('transcript_id') if p.get('projection_version') == PROJECTION_VERSION else None,
            delivery=p.get('delivery', 'provider_transcript_not_playback_receipt'))
        # A reused provider ID can have several bounded paragraphs. Object
        # identity preserves earlier text when a gap/length starts a new one.
        if role not in rows or rows[role][0] is not item:
            row = {'id': event['id'], 'kind': 'live_transcript', 'at': event['created'],
                   'data': {'role': role, 'text': '', 'playback_verified': False,
                            'speaker':speaker,
                            'boundary':'provisional',
                            'sources': [], 'projection_version': PROJECTION_VERSION}}
            rows[role] = (item,row)
            result.append(row)
        row = rows[role][1]
        row['data']['text'] = item['text']
        row['data']['sources'].append(event['id'])
        if p.get('delivery') == 'withheld':
            row['data']['delivery'] = 'withheld'
    return result
