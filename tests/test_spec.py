"""Unit tests for simulation-spec validation (no solver/CAE needed)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from abaqus_mcp.spec import (
    example_parametric_spec, example_spec, validate_spec,
)


def test_example_is_valid():
    assert validate_spec(example_spec()) == []
    print("OK example spec is valid")


def test_parametric_example_is_valid():
    assert validate_spec(example_parametric_spec()) == []
    print("OK parametric example spec is valid")


def test_parametric_missing_param():
    spec = example_parametric_spec()
    del spec["geometry"]["params"]["notch_radius"]
    errs = validate_spec(spec)
    assert any("notch_radius" in e for e in errs), errs
    print("OK missing parametric param caught")


def test_parametric_bad_shape():
    spec = example_parametric_spec()
    spec["geometry"]["shape"] = "banana"
    errs = validate_spec(spec)
    assert any("geometry.shape" in e for e in errs), errs
    print("OK bad parametric shape caught")


def test_missing_geometry():
    spec = example_spec()
    del spec["geometry"]
    errs = validate_spec(spec)
    assert any("geometry" in e for e in errs), errs
    print("OK missing geometry caught")


def test_bad_face_selector():
    spec = example_spec()
    spec["bcs"][0]["face"] = {"select": "topface"}  # not a valid selector
    errs = validate_spec(spec)
    assert any("face.select" in e for e in errs), errs
    print("OK bad face selector caught")


def test_section_material_must_exist():
    spec = example_spec()
    spec["section"]["material"] = "aluminum"  # not defined
    errs = validate_spec(spec)
    assert any("section.material" in e for e in errs), errs
    print("OK dangling section material caught")


def test_load_needs_type():
    spec = example_spec()
    spec["loads"][0].pop("type")
    errs = validate_spec(spec)
    assert any("loads[0].type" in e for e in errs), errs
    print("OK load type validated")


if __name__ == "__main__":
    test_example_is_valid()
    test_parametric_example_is_valid()
    test_parametric_missing_param()
    test_parametric_bad_shape()
    test_missing_geometry()
    test_bad_face_selector()
    test_section_material_must_exist()
    test_load_needs_type()
    print("\nAll spec validation tests passed.")
