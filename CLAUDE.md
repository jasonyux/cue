# Working in this repo

## Code layout: multi-line brackets

Any call, constructor, signature or literal that spans more than one line puts one argument per line and the closing
bracket alone on its own line. Never wrap arguments with the closing bracket at the end of the last one.

```python
inputs = PreferenceInput(
    now=window.end,
    window_start=window.start,
    input=given,
    settings=self.settings,
)
```

This applies to every file you write or edit, new modules included. Single-line calls stay on one line. Before handing
code over, check the files you touched for a construct that spans lines and closes on a shared line.
