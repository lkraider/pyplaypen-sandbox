# Manifesto

Working principles for this project and for agent work in general.

## Load-bearing only

Every line, dependency, sentence, and page element needs a reason to
be there. The reason can be functional, informational, or about how the
thing feels to use. Cut anything without one. Delete code that no longer
does useful work, whatever its age. No speculative abstraction, no unused
flexibility, no half-finished features.

## Minimal, low-level code

Fewer tokens and fewer lines mean fewer bugs and less overhead.
Lower-level code is faster and has less surface area. Prefer fewer
dependencies, and use a good existing one rather than rebuilding what it
already solves.

## One clear extension point

A library must not guess its caller's stack. Offer the smallest mechanism
that solves the real problem and let the caller supply the specifics.
Built-in behavior goes through the same extension point as user code;
that is the proof the mechanism works. A guarantee offered at the high
level must also be available at the lower level it is built from.

## Legibility

Two small pieces of code a reader can compare side by side teach a
pattern faster than one general abstraction the reader must learn first.
That can be worth extra lines. Name things by what they mean to the
person using them rather than by internal type or implementation.

## Code is truth

No separate docs. A comment explains a non-obvious why, never a what.
Paths not taken go in commit messages, where the context stays available
without cluttering the code. Commit often, in small logical units, with
messages that say why.

## Evidence

Reason from evidence and formal logic, never from vibes or guesses.
Check a claim by running it, rendering it, or reading its source. This
applies to code, prose, and visual design alike: a page is judged by
looking at it.

## Tests

A test exists to find ways the code fails, so it tries to break the code
rather than walk the easy path. Tests run fast. A slow test that adds
little gets deleted.

## Boundaries

State what is enforced, what is best effort, and what is unsupported.
A comparison with an alternative includes the real reasons to choose the
alternative.

## Machine and person

Language is code for the machine: text, code, and structured data it
parses directly. People are visual: they take in layout, diagrams, and
motion faster than paragraphs. A project meant for both carries both,
made with the same care. Animation must convey truth: direction and
timing match what actually happens, and elements fade rather than vanish.

## Corrections

When feedback shows an earlier version was closer to right, restore it
and add only the part that was missing. Size the fix to the actual
mistake.

## Language

Plain, calm, direct. Few words. No aphorisms, no filler, no rhetorical
packaging, no phrasing recognizable as generic AI writing. This applies
to the project's prose and to this document.
