# A retry that never retries

**Trigger:** you are adding or changing retry, backoff or reconnect logic.

**Trigger paths:** `**/retry*.*` `**/*backoff*.*`

**Trigger command:** `* --retries *`

**Class:** the retry loop is present, reads as correct, and can only ever
run once.

## What goes wrong

A retry wrapper is added and the code looks right: a loop, a delay, a bound.
But the error that should be retried is caught somewhere inside the loop and
turned into a return, or the condition is evaluated before the first attempt
rather than after, or the "attempts" counter is incremented in a branch that
never runs. The call succeeds on the happy path, so nothing looks wrong, and
the loop is exercised for the first time during the outage it was written for.

## Evidence

This page is an **example**, not a finding. It shows the shape a page takes
and the two kinds of trigger line; its evidence is invented. A real page
carries verbatim quotes with their source, because a claim nobody can check
is not evidence.

    fix(http): the retry loop returned inside its own except branch
    fix(sync): backoff slept before the first attempt, never after a failure

## Check before you trust a retry

- Make it fail on purpose. Point it at a closed port and watch the attempts
  in a log. A retry never observed retrying is not yet a retry.
- Count the attempts in a test, do not assert only the final outcome. A loop
  that runs once and a loop that runs five times both succeed eventually.
- Check what the loop catches. An exception handled inside the body never
  reaches the condition that would try again.
- Check the delay grows. A backoff that returns the same interval is a
  constant with extra arithmetic.
