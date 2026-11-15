# Remember a bridge configuration explicitly

`medre config use PATH` validates a bridge YAML and saves its absolute path in a
private selection pointer. `config current` shows the effective selection and
source; `config unset` removes only the pointer. Explicit paths and environment
selections retain precedence. Missing explicit or environment selections and
broken saved pointers fail without falling through to another bridge.
