"""Errors zclipper raises. The CLI prints them as one line on stderr."""


class ZClipperError(Exception):
    """A download, a platform request or an ffmpeg run failed."""


class NotFound(ZClipperError):
    """The video, stream or channel does not exist or was removed."""
