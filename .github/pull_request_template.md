<!--
Fill in every section. The middle two exist because the repo owner reviews risk,
not implementation — see AGENTS.md "Handing Code Work to Brian".
-->

## What this changes

<!-- One or two sentences, plain language. No internals. -->

## If this is wrong, what breaks — and how would you notice?

<!--
The load-bearing section. Translate the defect into observable consequences.

  Not: "mode='before' runs prior to extra-key checking"
  But: "a caller sending Google's own parameter names gets all-time search
        results, with HTTP 200 and no error — you would not notice"

Say explicitly if the failure would be SILENT. That is the case that matters.
-->

## How to undo this

<!--
Revert the merge? Redeploy a prior image? Is there a migration, a data change,
or anything else that does not come back with a revert? "Clean revert" is a
valid answer — say so.
-->

## Deliberately not changed

<!-- Scope boundaries, and anything noticed but left alone (with why). -->

## Verification

<!--
What was actually run, and its real output. Not "tests pass".
Include the command and the result line. Note any pre-existing failures and
whether they were confirmed pre-existing on an unmodified checkout.
-->

## Review status

- [ ] Has had an adversarial review pass
- [ ] Author is confident this is correct but it has NOT been adversarially reviewed

<!--
Tick one honestly. An unreviewed PR is fine — mislabelling one as reviewed is not.
-->
