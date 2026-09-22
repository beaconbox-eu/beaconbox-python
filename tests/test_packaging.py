"""Properties of the distribution itself, as opposed to the code inside it.

Everything here fails in a consumer's project rather than in this one, which is what makes it
worth asserting: the suite can be entirely green while the thing that gets installed is wrong.

Read through :mod:`importlib.metadata` rather than by parsing ``pyproject.toml``. That is the
metadata actually built into the distribution — the same thing PyPI serves and a resolver reads —
so a build backend that drops or rewrites a field is visible here, where parsing the source file
would only ever confirm what the source file says. It is also stdlib on every supported version,
which the alternative was not: reading the TOML needed ``tomli`` below 3.11, and that
version-conditional import under a ``python_version``-pinned mypy failed on 3.11, 3.12 and 3.13.
"""

from __future__ import annotations

import re
from importlib.metadata import metadata, requires, version
from pathlib import Path

import beaconbox
from beaconbox import __version__

_DISTRIBUTION = "beaconbox"

#: PEP 440, narrowed to the forms this project would actually publish.
_PEP440 = re.compile(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?")

#: A `<` bound whose value is a bare major version: `<1`, `<1.0`, `<1.0.0`.
_MAJOR_CEILING = re.compile(r"<\s*\d+(?:\.0+)*(?=[,\s]|$)")


def _requirement_name(dep: str) -> str:
    """The distribution name out of a PEP 508 requirement.

    Split on the first character that can end a name — an extra, a comparator, a marker or
    whitespace — rather than on `>=` alone, which silently returns the whole string for a
    perfectly ordinary `httpx<2` or `httpx ~= 0.28` and makes this test fail for the wrong
    reason.
    """
    return re.split(r"[\s<>=!~\[;]", dep, maxsplit=1)[0]


class TestTypeMarker:
    def test_py_typed_is_present(self) -> None:
        """Without it, a consumer running mypy gets **no** types from this SDK.

        PEP 561: a type checker ignores a dependency's annotations entirely unless the package
        ships this marker, silently and with no error. Every annotation in `src/` would be
        decoration, the `Typing :: Typed` classifier would be a false claim, and the only symptom
        is that somebody else's type checker stops catching their mistakes.

        **This checks the package as imported, which under the editable install the suite runs on
        is the source tree — so it catches the file being deleted and not a build configuration
        that stops shipping it.** Those are different failures and only one of them is visible
        from here; the wheel is inspected by the publish workflow and by CI's build step, which
        are the only places an artefact exists to look at.
        """
        marker = Path(beaconbox.__file__).parent / "py.typed"

        assert marker.is_file(), f"py.typed is missing from {marker.parent}"

    def test_the_typed_classifier_is_declared(self) -> None:
        """The marker and the classifier are two separate claims and both have to be true: the
        marker is what tooling reads, the classifier is what a person reads on PyPI."""
        classifiers = metadata(_DISTRIBUTION).get_all("Classifier") or []

        assert "Typing :: Typed" in classifiers


class TestVersion:
    def test_the_version_is_a_release_number(self) -> None:
        """Read by hatchling for the distribution and sent in the User-Agent on every request, so
        a malformed value is both an unpublishable package and a malformed header.

        **Pre-releases are allowed, in PEP 440 spelling** (`1.0.0rc1`, not `1.0.0-rc.1`). An
        earlier version of this test demanded exactly three numeric parts, which would have
        blocked a release candidate — and quietly disagreed with `sdk/scripts/release.sh`, whose
        version regex is semver-shaped and accepts the hyphenated form Python cannot use.
        """
        assert _PEP440.fullmatch(__version__), (
            f"{__version__!r} is not a PEP 440 release number. Expected MAJOR.MINOR.PATCH, "
            "optionally with a pre-release (1.0.0rc1), post (.post1) or dev (.dev1) segment."
        )

    def test_the_distribution_and_the_module_report_the_same_version(self) -> None:
        """`pyproject.toml` declares `dynamic = ["version"]` and hatchling reads `_version.py`, so
        these are one value by construction. Asserted anyway, because the failure is invisible: a
        literal `version = "…"` added to `pyproject.toml` would shadow the module, PyPI would
        serve that number, and every request would go on reporting the module's in its User-Agent.
        """
        assert version(_DISTRIBUTION) == __version__


class TestDependencies:
    def test_the_only_runtime_dependency_is_httpx(self) -> None:
        """A client library is installed *next to* an application's own dependencies, so every
        one it adds is a constraint on somebody else's resolver."""
        declared = requires(_DISTRIBUTION) or []

        assert [_requirement_name(dep) for dep in declared] == ["httpx"]

    def test_httpx_is_not_pinned_to_a_narrow_range(self) -> None:
        """A tight upper bound makes this SDK the reason an application cannot upgrade httpx —
        or cannot install at all, if something else already required a newer one."""
        spec = next(dep for dep in requires(_DISTRIBUTION) or [] if "httpx" in dep)

        # An upper bound that is a bare major — `<1`, `<1.0`, `<1.0.0`. A bound like `<0.29`
        # pins a *minor*, which is what makes a library uninstallable beside anything that
        # already needs a newer one. A substring test for "<1" would also accept `<15`.
        assert _MAJOR_CEILING.search(spec), (
            f"expected a major-version ceiling such as '<1', got {spec!r}"
        )

    def test_the_supported_python_floor_is_declared(self) -> None:
        """Without `Requires-Python`, pip installs this into an interpreter it does not run on and
        the failure is an import error rather than a resolution one."""
        floor = metadata(_DISTRIBUTION)["Requires-Python"]

        assert floor and floor.startswith(">="), f"expected a floor, got {floor!r}"
