Fixture for scripts/rh_decls: a tiny stand-in for riemann-hypothesis, laid out like it (a
`lakefile.toml` with `srcDir = "src"` and `defaultTargets`, `module` files with `@[expose] public
section`, and Comparator's `Challenge` outside the default libraries). Each file holds the cases
test_build.py checks; it is read as text and never compiled.
