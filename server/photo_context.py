"""Metadata and bounded reads for the current, committed photo observation."""

from datetime import datetime, timezone
import re
import time
from uuid import uuid4


PHOTO_MAX_AGE = 120
PHOTO_EXCERPT_CHARS = 800
_IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')


def stamp_observation(frame, observation):
    """Assign a new result identity even when a photo is inspected again."""
    return {
        **observation,
        'observation_id': uuid4().hex,
        'recognized_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'recognized_monotonic': time.monotonic(),
        'received_at': frame.get('received_at', ''),
        'capture_time_source': frame.get('capture_time_source', 'received'),
        'camera_epoch': frame['camera_epoch'],
    }


def current_observation(session, frame, camera_epoch):
    """Expose only a result for the current capture and current lesson state."""
    observation = session.observation
    if not session.active or not session.camera_enabled or not frame or not observation:
        return None
    if (
        observation.get('capture_id') != frame['id']
        or frame['camera_epoch'] != camera_epoch
        or observation.get('camera_epoch', camera_epoch) != camera_epoch
        or observation.get('generation') != session.generation
        or observation.get('plan_revision') != session.plan_revision
        or observation.get('withdrawn')
        or time.monotonic() - observation.get('captured_received', 0) > PHOTO_MAX_AGE
    ):
        return None
    return observation


def photo_reference(observation):
    """Return a logical reference, with no filesystem path or observation text."""
    if not observation:
        return None
    capture_id = observation.get('capture_id', '')
    observation_id = observation.get('observation_id', '')
    if not all(isinstance(value, str) and _IDENTIFIER.fullmatch(value) for value in (capture_id, observation_id)):
        return None
    return {
        'ref': f'obs://{capture_id}/{observation_id}',
        'capture_id': capture_id,
        'captured_at': observation.get('captured_at', '')[:40],
        'capture_time_source': observation.get('capture_time_source', 'received'),
        'recognized_at': observation.get('recognized_at', '')[:40],
    }


def photo_excerpt(text):
    return {'observation': text[:PHOTO_EXCERPT_CHARS], 'truncated': len(text) > PHOTO_EXCERPT_CHARS}


def read_photo_observation(observation, ref):
    photo = photo_reference(observation)
    if not photo or ref != photo['ref']:
        return {'available': False, 'photo': photo, 'reason': '照片結果已更新或不可用，請重新核對。'}
    return {'available': True, 'photo': photo, **photo_excerpt(observation['text'])}
