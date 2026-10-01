# Coding style

Write code that is easy to read on the first pass. Prefer the simplest solution that meets the requirement.

## Keep it simple

- Use clear names and direct control flow. A reader should not need to jump through several helpers to understand a short operation.
- Prefer one-line statements and expressions when they fit comfortably within the project's formatting limits. Avoid wrapping a short call or error message across multiple lines just for style.
- Keep functions focused and small when that makes their purpose easier to see. Avoid splitting code into helpers that only hide a simple expression.
- Prefer a few meaningful parameters. When several values always travel together, pass one existing domain object or a small dataclass instead of a long list of related arguments. Do not add abstractions just to reduce a parameter count.
- Avoid clever tricks, unnecessary layers, speculative flexibility, and duplicate representations of the same data.
- Add comments only when they explain a reason or constraint that the code cannot make clear by itself.

## Checks and errors

- Check data at boundaries where it is untrusted or can be absent: request payloads, environment settings, files, and external service responses.
- Once a value has been validated or represented by a typed domain object, trust it internally. Do not repeat the same checks at every call site.
- Add a check when it prevents a realistic failure or gives a useful error. Avoid checks for impossible states and defensive fallback paths without a concrete need.
- Let errors remain visible. Catch an exception only when the code can handle it, add useful context, or translate it into the right boundary response.

## Python conventions

- Follow the conventions already used in the surrounding code.
- Use type hints for function inputs and outputs, and docstrings for public functions when their purpose is not obvious.
- Prefer standard library and existing project dependencies over adding a dependency for a small task.
- Keep I/O and framework details near the boundary; keep domain logic straightforward and independently understandable.

## Changes

- Make the smallest complete change that solves the requested problem.
- Reuse the existing project patterns before introducing a new one.
- Update or add tests when behavior changes, following the existing test style.
