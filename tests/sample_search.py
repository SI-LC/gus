"""A search set up by hand, with every setting that has no default for a new install. The tests of how matching behaves
use it, so they don't depend on defaults, and it shows settings carried over from older installs keep working."""
from unittest import mock

from radar import settings

SEARCH = {
    "title_include": "product manager\nproduct lead\nhead of product\ngroup product\nprincipal product\ndirector of product\nvp product\nproduct owner\nproduct engineer\nproduct builder\nfounding product\nforward deployed\ndeployed staff\ndeployed engineer\ndeployed pm\napplied ai",
    "title_exclude": "marketing\ndesigner\ndesign\nengineer\nengineering manager\noperations\nanalyst\nintern\nsales",
    "title_exclude_except": "product engineer\ndeployed engineer\napplied ai engineer",
    "location_include": "london\nunited kingdom\nuk\nengland\nemea\neurope\nremote (exactly)\nremote global\nremote worldwide\nremote anywhere",
    "where_groups": "London: london\nRemote: remote, anywhere\nUK: united kingdom, uk, england, scotland, wales\nEurope: *",
    "role_kinds": "Forward deployed: forward deployed, forward-deployed, deployed staff, deployed engineer, deployed pm\nPM: product manager\n"
                  "Product engineer: product engineer, product builder, founding product, applied ai\nPM: *",
    "sources_location": "London", "sources_country": "gb", "salary_symbol": "£",
    "sources_searches": "product manager\nproduct lead",
    "hn_title_include": "(product manager)",
}


def loaded() -> dict:
    """The shipped defaults plus the sample, never the candidate's own settings.json: a test must run the same on
    the Mac, the Pi and GitHub, and a switch turned on in the real search (e.g. Google Careers) mustn't leak in."""
    return {**settings._read(settings.DEFAULTS), **SEARCH}


def use_sample(test) -> None:
    """Call in setUp."""
    patcher = mock.patch("radar.scan.load", loaded)
    patcher.start()
    test.addCleanup(patcher.stop)
