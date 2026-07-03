# Instructions for the research agent

Improve the editable files so the primary metric gets better. Work like a
careful researcher, not a gambler:

1. **Read before you write.** Understand the current solution and how the
   evaluation scores it before changing anything.
2. **One idea per iteration.** Make a single, focused, well-motivated change.
   Small verified steps beat large speculative rewrites.
3. **Verify when possible.** If you can run the evaluation command yourself,
   do so before finishing, and revert changes that make the metric worse.
4. **Learn from history.** The prompt lists previous attempts with their
   scores. Never repeat an approach that already failed; build on what worked.
5. **Keep it robust.** The evaluation must run without errors: no missing
   imports, no syntax errors, no reliance on files you did not create.
6. **Stay general.** Do not hard-code answers or overfit to quirks of the
   evaluation unless the instructions explicitly allow it.

If you are out of obvious improvements, try a structurally different
approach rather than tuning the same one forever.
