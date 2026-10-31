# Independent content owners and bounded quote grants

- Ordinary target generations can authorize retained content via their own live
  target-scoped hold after ingress source settlement; replay borrows only an
  existing source hold without creating new retention.
- Previously issued optional grants survive unrelated source completion, while
  issuing a fresh grant still requires source authority. Reply text may borrow
  only a live source or an explicitly preissued bounded `reply_quotes` grant.
  Body-only quote grants withhold opaque native/custom metadata after source
  settlement, preserving envelope identity without broadening the grant scope.
- Grant expiry and manual revocation have independent, irreversible timestamps
  in the unsupported prerelease schema; backward clock changes cannot restore
  expired read permissions. Rejected grant replacements and policy changes
  preserve observed expiry while rolling back the requested mutation.
  No automatic deletion is enabled.
