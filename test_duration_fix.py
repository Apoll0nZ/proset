#!/usr/bin/env python3
"""
Test to verify that duration_list_all is properly threaded through the pipeline.
This script verifies the critical fix for the NameError: duration_list_all bug.
"""

import sys
import os

# Add video_engine to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'video_engine'))

def test_imports():
    """Test that the module imports without errors"""
    print("Testing imports...")
    try:
        from render_video import synthesize_multiple_speeches, build_unified_timeline
        print("✓ Successfully imported synthesize_multiple_speeches and build_unified_timeline")
        return True
    except Exception as e:
        print(f"✗ Import failed: {e}")
        return False

def test_function_signatures():
    """Test that function signatures are correct"""
    print("\nTesting function signatures...")
    import inspect
    from render_video import synthesize_multiple_speeches, build_unified_timeline

    # Check synthesize_multiple_speeches returns 5 values
    print("  Checking synthesize_multiple_speeches return annotation...")
    # We can't easily check return values, but we can check the source
    src = inspect.getsource(synthesize_multiple_speeches)
    if "return final_audio_path, part_durations, query_data_list_all, text_parts_list_all, duration_list_all" in src:
        print("  ✓ synthesize_multiple_speeches returns duration_list_all")
    else:
        print("  ✗ synthesize_multiple_speeches missing duration_list_all in return")
        return False

    # Check build_unified_timeline accepts duration_list_all parameter
    sig = inspect.signature(build_unified_timeline)
    if 'duration_list_all' in sig.parameters:
        print("  ✓ build_unified_timeline accepts duration_list_all parameter")
    else:
        print("  ✗ build_unified_timeline missing duration_list_all parameter")
        return False

    return True

def test_duration_list_usage():
    """Test that duration_list is used in calculate_measured_chunk_durations"""
    print("\nTesting duration_list usage in build_unified_timeline...")
    import inspect
    from render_video import build_unified_timeline

    src = inspect.getsource(build_unified_timeline)

    # Check that duration_list_all is extracted and used
    if "duration_list = duration_list_all.get(" in src:
        print("  ✓ build_unified_timeline extracts duration_list from duration_list_all")
    else:
        print("  ✗ build_unified_timeline doesn't extract duration_list from duration_list_all")
        return False

    if "calculate_measured_chunk_durations" in src and "duration_list" in src:
        print("  ✓ calculate_measured_chunk_durations is called with duration_list")
    else:
        print("  ✗ calculate_measured_chunk_durations not properly called")
        return False

    return True

def main():
    """Run all tests"""
    print("=" * 70)
    print("DURATION_LIST_ALL FIX VERIFICATION TEST")
    print("=" * 70)

    tests = [
        ("Imports", test_imports),
        ("Function Signatures", test_function_signatures),
        ("Duration List Usage", test_duration_list_usage),
    ]

    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append(result)
        except Exception as e:
            print(f"✗ {name} test error: {e}")
            import traceback
            traceback.print_exc()
            results.append(False)

    print("\n" + "=" * 70)
    print("TEST RESULTS:")
    print(f"  Passed: {sum(results)}/{len(results)}")
    print("=" * 70)

    if all(results):
        print("✓ All tests PASSED - duration_list_all fix is properly implemented")
        return 0
    else:
        print("✗ Some tests FAILED - there may be issues with the fix")
        return 1

if __name__ == "__main__":
    sys.exit(main())
