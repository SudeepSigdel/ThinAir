# Test fixtures

Sample projects used by Thinair's Docker tests. Each has a test that deliberately holds 100-150 MB
of memory, so the profiler's expected minimum is the 256m tier (128m should fail with an OOM kill).
These are never collected by the main test suite (`--ignore=tests/fixtures`).
