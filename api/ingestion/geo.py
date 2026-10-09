"""
Straight-line distance, computed in the database so the feed can filter and
sort by it without loading every posting.

Haversine on a spherical Earth: accurate to well under a percent at commuting
distances, which is far finer than "is this 25 or 50 miles away" needs.
"""

import math

from django.db.models import F, FloatField, Value
from django.db.models.functions import ASin, Cos, Least, Power, Radians, Sin, Sqrt

EARTH_RADIUS_MILES = 3958.8


def miles_from(latitude, longitude, lat_field="latitude", lng_field="longitude"):
    """An expression for the miles between (latitude, longitude) and each row."""
    origin_lat = Value(math.radians(latitude), output_field=FloatField())
    origin_lng = Value(math.radians(longitude), output_field=FloatField())
    row_lat = Radians(F(lat_field))
    row_lng = Radians(F(lng_field))

    half_chord = (
        Power(Sin((row_lat - origin_lat) / 2), 2)
        + Cos(origin_lat) * Cos(row_lat) * Power(Sin((row_lng - origin_lng) / 2), 2)
    )
    # Rounding can push the term a hair past 1 for antipodal points, and asin
    # of anything over 1 is an error in Postgres rather than a NaN.
    return Value(2 * EARTH_RADIUS_MILES, output_field=FloatField()) * ASin(
        Sqrt(Least(half_chord, Value(1.0, output_field=FloatField())))
    )


def miles_between(lat1, lng1, lat2, lng2):
    """The same calculation in Python, for tests and one-off checks."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = phi2 - phi1, math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(min(1.0, math.sqrt(a)))
