# Capture resolved stopped-state selections under retained exclusion

Add `capture-config` as an explicit complete-selection maintenance path:
acquire provider ownership, resolve read-only native metadata while held,
validate the selected files, reject unresolved exports, and publish the
snapshot before releasing exclusion. Keep `plan` configuration-only and the
existing `capture --inventory` operator-selected rather than implying
that a prior standalone `check` grants continuous ownership.
