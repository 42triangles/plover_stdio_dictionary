# Plover `stdio` dictionary

Add support for dictionaries written in an arbitrary language communicating via stdio.

This is a fork of [`benoit-pierre/plover_python_dictionary`][1] at version 1.1.0.


[1]: https://github.com/benoit-pierre/plover_python_dictionary


## Usage

An stdio dictionary is a shell script, ran in `~/.local/share/plover`

```
#!/bin/sh
./some-dictionary-binary TEFT
```

## Protocol
The protocol uses JSON for everything except error reporting, with one json object per line.

The script first outputs its configuration, in the following form:
```
{"longest-key": 5, "max-latency-ms": 100, "untranslate": true}
```

Default values:
* `max-latency-ms`: `null` (`null` means it will potentially block forever)
* `untranslate`: `false`

Afterwards it'll receive stroke sequences like
```
{"seq": 0, "translate": ["TH", "S", "AEU", "TEFT"]}
```
or
```
{"seq": 0, "untranslate": "this is a test"}
```

The response should be an object with `seq` matching the `seq` value of the input.

Response keys (all optional):
* `translation` (for `translate`): The text for a successful translation, if applicable
* `reverse-translation` (for `untranslate`): The list of stroke sequences for a successful reverse lookup, if applicable

Any output on stderr is relayed back to Plover as an exception, per line.

## Release history

### 0.1
* Initial release
