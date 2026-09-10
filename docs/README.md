# Documentation

Start with the [README](../README.md) for what to deploy, then the
[local tutorial](developer/router-tutorial.md) to run the gateway and consoles.
The [developer guide](developer/index.rst) covers installation, configuration,
routing, operations and contributing. Its English and Chinese editions are
built and checked together with `make docs-verify`.

The other directories hold engineering records:

| Directory | How to read it |
| --- | --- |
| `agents/specs/`, `agents/plans/`, `superpowers/` | Proposals and implementation records. A plan's commands and checkboxes describe that work, not the current installation procedure. |
| `agents/specs/archive/`, `agents/plans/archive/` | Archived records retained for context. |
| `reviews/` | Findings against the version reviewed on the stated date. They are not a current release assessment. |

Historical filenames and line numbers can outlive the code they describe.
Links to surviving files are maintained for navigation; references to removed
files remain as text. Use the developer guide for current behavior and setup.
