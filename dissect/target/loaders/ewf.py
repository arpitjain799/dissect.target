from dissect.target import container
from dissect.target.loader import Loader


class EwfLoader(Loader):
    """Loader for Encase EWF forensic image format files."""

    @staticmethod
    def detect(path):
        return path.suffix.lower() in (".e01", ".s01", ".l01")

    def map(self, target):
        target.disks.add(container.open(self.path))
