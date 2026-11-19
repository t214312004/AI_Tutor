"""Useful failures without logging prompts, environment values or account paths."""
import logging
import os
from pathlib import Path
import traceback

logger = logging.getLogger('tutor.transport')


def failure_details(operation, error, path=None):
    path=path or getattr(error,'filename',None)
    return {'operation': operation, 'error_type': type(error).__name__,
            'errno': getattr(error, 'errno', None), 'winerror': getattr(error, 'winerror', None),
            'file': Path(path).name if isinstance(path, (str, os.PathLike)) and path else None,
            'frames': [f'{Path(frame.filename).name}:{frame.lineno}:{frame.name}'
                       for frame in traceback.extract_tb(error.__traceback__)[-8:]]}


def report_failure(operation, error, path=None):
    details = failure_details(operation, error, path)
    logger.error('operation_failed %s', details)
    return details
