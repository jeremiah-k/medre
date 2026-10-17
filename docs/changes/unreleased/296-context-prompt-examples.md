# Context prompts show the provider's example address

Interactive `medre config bridge` context prompts display the selected
adapter's declared sample conversation address, e.g. `sample_1 context
(e.g. '!room:example.com')`. Providers already declare the address on
their sample configuration; no new contract surface is introduced. A
provider whose sample callback fails keeps working prompts without an
example.
