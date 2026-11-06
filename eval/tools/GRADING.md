# Grading rules

These were fixed before any held-out answer was read.

Each answer is graded on its text alone, against the question's EXPECTED note, without knowing which configuration wrote it. The grader is Claude, so blinding hides the configuration label but not differences in style. The labels are a judgement, not a measurement.

Answerable questions:

- **correct**: says the main point or points of EXPECTED, and nothing in it is false. Extra true detail is fine. Clumsy Turkish is fine if the meaning is clear.
- **partial**: gets at least one real part of EXPECTED, but misses another main part, or also contains a false or off-topic claim that does not reverse the main point.
- **wrong**: the main claim is false or contradicts EXPECTED, or the answer does not address what was asked, or it is too garbled to understand.

When an answer shows code lines, a line that visibly shows a main point counts as saying it, because a reader can see it. A false sentence next to them still lowers the grade to partial, even when it is labelled as unverified. Added before the pilot's answers existed, for answers made of code lines.

Unanswerable questions, where the repository does not have the feature:

- **fabricated**: says or implies that the repository has the feature.
- **harmless**: true statements about nearby code that do not claim the feature exists. This is still a failure to refuse.

Refusals are not graded. They are counted from the status field. A refused answerable question is a false refusal, reported on its own, and never counts as correct.

If a note and an answer disagree and the note may be too narrow, check the code at the pinned commit. Do not change a question, a note or a gold span because of an answer. If a note turns out to be wrong, report it and leave it.
