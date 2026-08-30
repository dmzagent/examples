# examples

Runnable examples for the DMZAgent SDKs.

This repository is scaffolding. It carries a licence, a security policy and this
README so that it is not an unexplained empty repository in the organisation,
but it does not yet hold any examples.

## What belongs here

Small, self-contained programs that show one thing each, in any of the four
supported languages:

| SDK | Repository |
| --- | --- |
| Python | [`dmzagent/dmzagent-sdk-python`](https://github.com/dmzagent/dmzagent-sdk-python) |
| TypeScript | [`dmzagent/dmzagent-sdk-typescript`](https://github.com/dmzagent/dmzagent-sdk-typescript) |
| Java | [`dmzagent/dmzagent-sdk-java`](https://github.com/dmzagent/dmzagent-sdk-java) |
| C# | [`dmzagent/dmzagent-sdk-csharp`](https://github.com/dmzagent/dmzagent-sdk-csharp) |

The wire contract every SDK implements lives in
[`dmzagent/dmzagent-sdk-spec`](https://github.com/dmzagent/dmzagent-sdk-spec).

## What does not belong here

Anything that has to stay in step with the wire contract. Conformance is proved
by the shared vectors in `dmzagent-sdk-spec`, which every SDK runs in its own
`spec-conformance` workflow. An example that duplicates that logic will drift
without anything noticing.

## Conventions for a new example

- One directory per example, named for what it demonstrates.
- A `README.md` that states what it shows and how to run it.
- Pin the SDK version explicitly, so the example keeps working when the SDK moves.
- No credentials in source. Read them from the environment.

## Status

If this repository is still empty when you read this, it is a candidate for
deletion rather than a gap to fill — the SDK repositories each carry their own
quickstart, and that may be sufficient.
