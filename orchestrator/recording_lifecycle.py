"""Capture ownership and encoder completion are separate recording milestones."""
from dataclasses import dataclass


def pending_recording(saved: dict | None) -> bool:
    return bool(saved and saved.get('state') in {'recording', 'recovering', 'finishing'})


@dataclass
class RecordingStop:
    requested: bool = False
    capture_stopped: bool = False
    notified: bool = False
    encoder_notified: bool = False

    def observe(self, clock: dict, plugin: dict) -> bool:
        """Return encoder completion; never infer capture release from a stop request."""
        stopped = (not clock.get('lockstep')
                   and plugin.get('state') in {'idle', 'finishing', 'finished', 'failed'}
                   and plugin.get('recording') is not True)
        # A later snapshot that starts capture again belongs to a different lifecycle.
        if self.capture_stopped and not stopped:
            raise RuntimeError('录制采集在收尾期间重新启动，拒绝继续处理')
        self.capture_stopped = stopped
        return stopped and plugin.get('state') in {'finished', 'failed'}

    def notify(self, callback) -> None:
        if self.capture_stopped and not self.notified and callback is not None:
            callback()
            self.notified = True

    def notify_encoder(self, callback) -> None:
        if callback is not None and not self.encoder_notified:
            callback()
            self.encoder_notified = True
