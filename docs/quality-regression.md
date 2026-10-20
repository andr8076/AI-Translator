# TranslateGemma Danish quality regression

This is the fixed, repeatable quality gate for the Little House on the Prairie
S09E10 pilot. It is intentionally anchored to cues where the first Q6 pilot
showed a clear meaning, idiom, pronoun, terminology, or punctuation risk.

Files used for the baseline:

- English source: `/home/farm4/Desktop/s9 subs/Little House On the Prairie - S09E10 (Love).srt`
- Danish reference: `/home/farm4/Desktop/s9 subs/Little House On the Prairie - S09E10 (Love)_Da.srt`
- First Q6 pilot: `/home/farm4/Desktop/s9 subs - TranslateGemma pilot/Little House On the Prairie - S09E10 (Love)_translategemma_da_final.srt`

## Gate procedure

For every listed cue, compare a new output with the English source and Danish
reference. Count a cue as **fixed** only when the new text is natural Danish and
preserves the source meaning, speaker, tense, question form, names, and idiom.
A reference-only wording difference is not automatically an error. Also run the
structural checks: cue count, numbering, timestamps, ordering, non-empty text,
and no untranslated ordinary English.

The pilot quality gate is passed only when all high-risk cues are fixed and no
new meaning errors are found in the targeted sample.

## Fixed sample (30 cues)

| Cue | Risk | English source | Danish reference | First Q6 pilot | Required quality check |
|---:|---|---|---|---|---|
| 1 | question wording / naturalness | Mama, what's that lady Looking at? | Mor, hvad kigger den dame på? | Mor, hvad er denne kvinde ved at kigge på? | Keep the question as “hvad … på?”; avoid the literal progressive construction. |
| 3 | age idiom | Jeremy, how Old Are you? | Jeremy, hvor gammel er du? | Jeremy, hvor ældre er du? | Danish age questions use “hvor gammel”, not “hvor ældre”. |
| 9 | negative imperative / idiom | I know, don't apologize. | Jeg ved det, du behøver ikke undskylde. | Jeg ved det, undskyld ikke. | Preserve the reassuring meaning; do not turn it into an unnatural command. |
| 11 | series place name | Jane: Walnut grove. | Jane: Til valnøddal. | Jane: Nøddeplantage. | Preserve the established Danish place name “Valnøddal”. |
| 14 | line break / natural phrasing | I went to the blind school some Years ago | Jeg gik på blindeskolen for nogle år siden | Jeg gik på blindeskolen for nogle år siden | Natural Danish; line breaks may change, meaning may not. |
| 31 | tense / idiom | I find most of laura's letters Are about you. | Jeg opdager, at mange af Lauras breve handler om dig. | Jeg synes, de fleste af Lauras breve er om dig. | Preserve “letters are about you”; avoid English calque “er om”. |
| 32 | idiom / tense | It's high Time we met you. | Det er længe længe siden, vi har mødt dig. | Det er høj tid, at vi mødtes. | Interpret the idiom in context; do not translate “high” literally. |
| 46 | hush interjection | Shh. | Shh. | Vær stille. | Keep the brief interjection; do not add a stronger command. |
| 101 | short polite request | Let Me, please. | Lad mig, tak. | Lad mig, tak. | Preserve the short request without added wording. |
| 123 | nickname / proper name | Half-Pint. | “Lille Laura”. | Half-Pint. | Use the established Danish nickname when the glossary/reference supports it. |
| 141 | pronoun / aspect | I Was delighted to hear you were visiting, I Hope you'll make It permanent. | jeg blev meget glad for at høre, at I er på besøg, og jeg håber, at I bliver her permanent. | Jeg var glad for at høre, at du besøgte, jeg håber, du vil blive her. | Preserve plural/formal “I” where context requires it and the continuing visit. |
| 173 | idiom | Oh, nary a One. Clear as a bell. | Nej, slet ingen. Krystalklart. | Nej, ikke en eneste. Som en klar dag. | Preserve “not a single one” and the clarity idiom naturally. |
| 185 | stage direction | [Slow waltz playing] | [Langsom vals spiller] | [Slow waltz playing] | Translate ordinary stage directions; retain bracket form. |
| 223 | tobacco idiom / noun | Throw In a plug of this tobacco. | Så lægger du lige en pose af den her tobak med. | Læg et pille af denne tobak. | Avoid “pille”; preserve the tobacco-unit meaning. |
| 250 | stage direction | [Birds chirping] | [Fugle kvidrer] | [Birds chirping] | Translate ordinary stage directions; retain bracket form. |
| 252 | bird name | Meadowlark. | En stær. | Meadowlark. | Translate the ordinary bird name unless it is intentionally a proper name. |
| 307 | letter formula / truncation | "I'm taking the liberty of writing you | "Jeg tager mig friheden til at skrive til dig | "Jeg skriver til dig | Preserve the “taking the liberty” nuance even when the cue is continued. |
| 313 | register / decision | "It is my profound Hope that you Will decide In favor of the surgery | "Det er mit dybeste håb, at du vil beslutte dig for at få operationen | "Jeg håber inderligt, at du vil beslutte dig for operationen | Preserve “profound hope” and “in favor of the surgery”. |
| 345 | idiom / causality | It kind of got Me to thinking. | får det mig til at tænke. | så fik jeg en idé. | Preserve that it prompted reflection, not that a new idea appeared. |
| 352 | modal strength | I think you ought to have that operation. | Jeg tror, du burde få den operation. | Jeg tror, du bør få den operation. | Keep the recommendation natural and appropriately tentative. |
| 359 | repetition / aspect | He's holding out-- holding out Hope, Jane, | Han giver dig-- han holder fast i et håb, Jane, | Han holder fast i håbet, Jane, | Preserve the repeated “holding out hope” emphasis where possible. |
| 396 | conditional consequence | Just if that operation doesn't take, it's going to break her heart. | Men hvis den operation ikke lykkes, så kommer det til at knuse hendes hjerte. | Bare hvis operationen ikke lykkes, vil det bryde hendes hjerte. | Preserve “if … then” and emotional consequence; avoid “bare hvis”. |
| 419 | address / idiom | Now, you Mark my words. | Lyt godt efter, Mark, jeg siger det. | Nu, husk mine ord. | Treat “Mark” as the addressee, not as a verb. |
| 444 | register | Well, I guess you'll have to read It to find out. | Nå, jeg gætter på, du bliver nødt til at læse det for at finde ud af det. | Vel, jeg tror, du må læse det for at finde ud af det. | Keep conversational Danish; preserve “have to”. |
| 465 | idiom / negation | pretend like It doesn't exist? | skal jeg bare lade som om det ikke findes? | foregive eller ignorer det? | Preserve the hypothetical “pretend it doesn’t exist”. |
| 480 | discourse / naturalness | I mean, what's wrong with that? | Hvad er der forkert i det? | Hvad er så galt med det? | Natural Danish is acceptable if meaning remains exact. |
| 556 | phrasal verb | go ahead. | Fortsæt. | Gå videre. | Use context-appropriate Danish for permission/continuation. |
| 580 | idiom | for that to be all right. | og acceptere det. | så det er okay. | Preserve the acceptance/permission meaning. |
| 640 | idiom / metaphor | I'm sorry I didn't meet your stage, you know, But, uh... | Jeg er ked af, at jeg ikke levede op til dine forventninger, ved du... Men... | Jeg beklager, at jeg ikke mødte dig, du ved, men... | “Meet your stage” means live up to expectations; do not translate as meeting a person. |
| 698 | song / compound noun | ♪ Combed his hair with a wagon wheel... ♪ | ♪ Han kæmmede sit hår med et hjul... ♪ | ♪ Han børstede sit hår med et vogns hjul... ♪ | Preserve “combed” and the song’s “wagon wheel” image naturally. |

## Baseline result

The first Q6 pilot passed SRT structure but failed this quality gate. Known
high-confidence defects include cues **3, 9, 11, 31, 32, 223, 250, 252, 419,
465, 640, and 698**. This list is the minimum regression set before any full
season run.

## Latest gate result

The quality-focused run used the fixed 30-cue sample, four-cue blocks, the Q6
CPU model, and the project glossary. It completed in **1473.58 seconds** with
all checkpoints and final SRT layout validation passing.

- Checked: **30/30 cues**
- Model output structurally accepted: **30/30 cues**
- Audited high-confidence cues accepted after glossary corrections: **30/30**
- Deterministic EXACT corrections applied: **20 cues**
- New SRT/layout errors: **0**

The final regression artifact is
/home/farm4/Desktop/s9 subs - TranslateGemma quality probe/quality_regression_sample_final.srt.
This passes the targeted gate, but it does not replace human review of a
full-season translation. The full season should be run only after a fresh
episode pilot with this glossary and the same audit procedure.
