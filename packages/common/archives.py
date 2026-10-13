"""Bound tar parser extensions before stdlib consumes their payloads.

Python 3.12 TarInfo processes PAX/GNU headers before yielding entries. Per-entry
limits applied by a caller therefore cannot prevent those allocations.
"""
import tarfile


def bounded_tar_info(metadata_limit: int = 16 * 1024**2, entry_limit: int = 100_000):
    class BoundedTarInfo(tarfile.TarInfo):
        def _proc_member(self, archive):
            count = getattr(archive, '_packferry_header_count', 0) + 1
            archive._packferry_header_count = count
            if count > entry_limit:
                raise ValueError('Tar header count limit exceeded')
            if self.type == tarfile.GNUTYPE_SPARSE:
                raise ValueError('Sparse tar members are not supported')
            if self.type in {tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK}:
                total = getattr(archive, '_packferry_extension_bytes', 0) + self.size
                archive._packferry_extension_bytes = total
                if self.size < 0 or total > metadata_limit:
                    raise ValueError('Tar extension metadata limit exceeded')
            return super()._proc_member(archive)

        def _proc_gnusparse_00(self, *args, **kwargs):
            raise ValueError('Sparse tar members are not supported')

        def _proc_gnusparse_01(self, *args, **kwargs):
            raise ValueError('Sparse tar members are not supported')

        def _proc_gnusparse_10(self, *args, **kwargs):
            raise ValueError('Sparse tar members are not supported')

    return BoundedTarInfo
