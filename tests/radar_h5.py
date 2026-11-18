"""Un fichier mosaïque radar HDF5 synthétique, avec les attributs du vrai fichier relevé le 18/11/2026."""

from __future__ import annotations

import h5py
import numpy as np

from app.radar_grid import SIZE

PROJDEF = b"+proj=stere +lat_0=90 +lon_0=0 +lat_ts=45 +ellps=WGS84 +x_0=619652.07 +y_0=5262818.34 +datum=WGS84 +units=m +no_defs"


def make_h5(
    data: np.ndarray | None = None,
    end: tuple[str, str] = ("20261118", "082500"),
    projdef: bytes = PROJDEF,
    quantity: bytes = b"ACRR",
) -> bytes:
    """Les octets d'un fichier `LAME_D_EAU` à 500 m ; `data` vaut « sec partout » (0) par défaut."""
    image = np.zeros((SIZE, SIZE), dtype=np.uint16) if data is None else data
    f = h5py.File("mosaique.h5", "w", driver="core", backing_store=False)
    where = f.create_group("where")
    where.attrs["projdef"] = projdef
    where.attrs["xsize"] = SIZE
    where.attrs["ysize"] = SIZE
    where.attrs["xscale"] = 500.0
    where.attrs["yscale"] = 500.0
    what = f.create_group("dataset1/data1/what")
    what.attrs["quantity"] = quantity
    what.attrs["gain"] = 0.01
    what.attrs["offset"] = 0.0
    what.attrs["nodata"] = 65535.0
    what.attrs["undetect"] = 65534.0
    f["dataset1/data1"].create_dataset("data", data=image, compression="gzip")
    when = f.create_group("dataset1/what")
    when.attrs["enddate"] = end[0].encode()
    when.attrs["endtime"] = end[1].encode()
    f.flush()
    raw = f.id.get_file_image()
    f.close()
    return raw
