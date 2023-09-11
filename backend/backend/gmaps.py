import os
import time
from typing import Iterable, TypedDict

import tqdm.auto as tqdm
import requests

from .location import Location, get_mercator_scale_factor


def get_api_key():
    return os.getenv("GMAPS_API_KEY")


def get_static_map(
    center: Location, zoom: int, markers: list[Location] | None = None
) -> bytes:
    if not 0 <= zoom <= 21:
        raise ValueError("Zoom must be between 0 and 21")

    if markers is None:
        markers = []

    params = {
        "center": center,
        "zoom": zoom,
        "size": "400x400",
        "key": get_api_key(),
        "markers": "|" + "|".join(str(x) for x in markers),
        "scale": 2,
    }
    params_s = "&".join([f"{k}={v}" for k, v in params.items()])
    response = requests.get(
        f"https://maps.googleapis.com/maps/api/staticmap?{params_s}"
    )
    response.raise_for_status()

    return response.content


def get_distance_matrix_api_payload(
    origins: list[Location], destinations: list[Location]
):
    return {
        "origins": [l.to_route_matrix_location() for l in origins],
        "destinations": [l.to_route_matrix_location() for l in destinations],
        "travelMode": "DRIVE",
        # Note: TRAFFIC_AWARE and TRAFFIC_AWARE_OPTIMAL are more expensive.
        # TRAFFIC_UNAWARE is the default.
        "routingPreference": "TRAFFIC_UNAWARE",
        # "travelMode": "TRANSIT",
        # 9:00 UTC is 11:00 CEST
        # "departureTime": "2023-09-04T09:00:00Z",
    }


def confirm_if_expensive(origins: list[Location], destinations: list[Location]):
    # Note: 1000 elements = 5 dollars
    # https://developers.google.com/maps/documentation/routes/usage-and-billing#rm-basic
    DOLLARS_PER_ELEMENT = 0.005
    n_entries = len(origins) * len(destinations)
    cost_dollars = n_entries * DOLLARS_PER_ELEMENT
    if cost_dollars >= 1:
        print(
            f"WARNING: You are asking for {n_entries} routes, "
            f"which will cost {cost_dollars}$.\n"
            "Do you want to continue? [y/N]"
        )
        if input().lower() != "y":
            raise RuntimeError("User is broke")


def call_distance_matrix_api(
    origins: list[Location], destinations: list[Location], confirm: bool = True
):
    if confirm:
        confirm_if_expensive(origins, destinations)

    data = get_distance_matrix_api_payload(origins, destinations)

    for attempt in range(3):
        response = requests.post(
            "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix",
            json=data,
            headers={
                "X-Goog-Api-Key": get_api_key(),
                "X-Goog-FieldMask": "originIndex,destinationIndex,"
                "duration,distanceMeters,status,condition",
            },
        )
        if response.status_code == 429:
            print("Rate limit exceeded, retrying...")
            time.sleep(30)
            continue

        response.raise_for_status()
        return response

    raise RuntimeError("Rate limit exceeded")


def get_distance_matrix(
    origins: list[Location], destinations: list[Location]
) -> Iterable[dict]:
    confirm_if_expensive(origins, destinations)

    ROOT_MAX_ENTRIES = 25
    MAX_ENTRIES = ROOT_MAX_ENTRIES * 2

    if len(origins) * len(destinations) > MAX_ENTRIES:
        for i in tqdm.trange(0, len(origins), ROOT_MAX_ENTRIES):
            for j in range(0, len(destinations), ROOT_MAX_ENTRIES):
                response = call_distance_matrix_api(
                    origins[i : i + ROOT_MAX_ENTRIES],
                    destinations[j : j + ROOT_MAX_ENTRIES],
                    confirm=False,  # Already confirmed above
                )

                # Reindex to match the original indices
                matrix_entries = response.json()
                for entry in matrix_entries:
                    # TODO: Some requests returned entries that didn't have
                    # originIndex or destinationIndex, but I couldn't reproduce.
                    if "originIndex" in entry:
                        entry["originIndex"] += i
                    if "destinationIndex" in entry:
                        entry["destinationIndex"] += j
                yield from matrix_entries
    else:
        response = call_distance_matrix_api(origins, destinations)
        yield from response.json()


class ResolvedLocation(TypedDict):
    location: Location
    types: list[str]


def snap_to_road(location: Location) -> ResolvedLocation:
    """Resolve a lan/lng pair to a location close to a road using reverse geocoding.

    This is useful for snapping points in unreachable locations, like bodies of water,
    to the closest road.
    """
    response = requests.get(
        f"https://maps.googleapis.com/maps/api/geocode/json?"
        f"latlng={location}&key={get_api_key()}"
    )
    data = response.json()

    if data["status"] != "OK":
        raise ValueError(f"Got non-OK status when resolving {location}. Got: {data}")

    # https://developers.google.com/maps/documentation/geocoding/requests-reverse-geocoding
    # The API returns multiple results - different descriptions for the location, like
    # street, city, country, and a bunch of more complicated ones. Filter to the accurate
    # ones.
    # "street_address" sounds like what you'd want, but it leaves some markers in the lake
    # so give higher priority to "route".
    result_types = ["route", "street_address", "point_of_interest"]

    resolution = None

    for result_type in result_types:
        filtered_results = [x for x in data["results"] if result_type in x["types"]]
        if filtered_results:
            resolution = filtered_results[0]
            break

    if resolution is None:
        raise ValueError(f"No location found when resolving {location}. Got: {data}")

    return {
        "location": Location(
            resolution["geometry"]["location"]["lat"],
            resolution["geometry"]["location"]["lng"],
        ),
        "place_id": resolution["place_id"],
        "types": resolution["types"],
    }


def linspace(a, b, n):
    return [a + (b - a) / (n - 1) * i for i in range(n)]


def make_grid(
    center: Location, zoom: int, size: int = 5, snap_to_roads: bool = True
) -> list[Location]:
    """Make a grid of locations around a center location, for plotting on a map."""

    # If place markers on the map returned get_static_map() such that you move
    # from the center by STATIC_MAP_SIZE_COEF (adjusted for zoom and Mercator)
    # in each "diagonal" direction, you will reach the four corners of the map.
    STATIC_MAP_SIZE_COEF = 280

    max_offset_lat = (
        STATIC_MAP_SIZE_COEF / (2**zoom) / get_mercator_scale_factor(center.lat)
    )

    max_offset_lng = STATIC_MAP_SIZE_COEF / (2**zoom)

    locations = []
    # Reverse the latitude so that the markers go "top to bottom" (north to south)
    for lat in reversed(
        linspace(center.lat - max_offset_lat, center.lat + max_offset_lat, size)
    ):
        for lng in linspace(
            center.lng - max_offset_lng, center.lng + max_offset_lng, size
        ):
            locations.append(Location(lat, lng))

    if snap_to_roads:
        locations = [
            snap_to_road(l)["location"]
            for l in tqdm.tqdm(locations, desc="Snapping to roads")
        ]

    return locations
