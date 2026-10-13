# pdfnotes: LaTeX notes that are also PDF annotations

`pdfnotes.sty` makes review notes written in LaTeX into real PDF annotations, while the
page keeps the look of [todonotes](https://ctan.org/pkg/todonotes):

- a **note** is a todonotes box and a Text (sticky-note) annotation over it, with the
  author, the note, a subject (todo, comment, done ...) and the colour;
- a **mark** is a Highlight, Underline, Squiggly or StrikeOut annotation over typeset text,
  across line, column and page breaks, with the note as its contents, plus (by default)
  the note in a todonotes box.

So every viewer lists them in its annotation panel (Okular, Acrobat, the VS Code viewer of
vscode-pdf-mcp), and an agent reads the open notes with this repository's `annotations`
tool. `sync_to_source` (see the main README, "LaTeX sources") goes the other way and writes
marks made in a viewer into the sources as these macros.

Requirements: LaTeX 2022-11 or newer with todonotes, xcolor and zref (all in TeX Live,
MiKTeX and on Overleaf). pdfLaTeX is the main engine; LuaLaTeX and XeLaTeX work too (see
[Limitations](#limitations)). In DVI mode the notes are typeset without annotations.

## Install

**For all your documents**: link this directory into your personal TeX tree and check that
TeX finds it:

```sh
mkdir -p "$(kpsewhich -var-value TEXMFHOME)/tex/latex"
ln -s <repo>/latex "$(kpsewhich -var-value TEXMFHOME)/tex/latex/pdfnotes"
kpsewhich pdfnotes.sty
```

(`<repo>` is your clone of pdf-reader-writer-mcp; `git pull` there updates the package.)

**For one paper**: copy `pdfnotes.sty` next to the paper's main `.tex` file and commit it.
A paper that also builds on **Overleaf** needs this, because there is no personal TeX tree
there.

## Usage

```latex
\usepackage{todonotes}
\presetkeys{todonotes}{inline,backgroundcolor=yellow,bordercolor=black,linecolor=cyan}{}
\usepackage{pdfnotes}
\newpdfnote{\todoalice}{author=Alice,color=pink,kind=todo}
\newpdfnote{\commentalice}{author=Alice,color=cyan,kind=comment,prefix={Alice: }}
\newpdfnote{\todobob}{author=Bob,color=orange,kind=todo,prefix={Bob: }}
\newpdfnote{\donealice}{author=Alice,color=green,kind=done}
\newpdfmark{\flagalice}{author=Alice,color=pink,textcolor=orange,kind=todo}
...
Regions are finite.\todoalice{Cite the region bound.}
\flagalice{This sentence is too long}{Split it.}
```

`\todoalice{...}` is `\todo[backgroundcolor=...]{...}` plus the annotation, and
`\flagalice{text}{note}` typesets `text` in orange with a highlight over it and the note
in a todonotes box. Any todonotes option passes through: `\todoalice[inline]{...}`.

Build with **two runs** (latexmk, LaTeX Workshop and Overleaf do this by themselves): the
annotations are placed from positions recorded in the `.aux` file in the run before, as
`\ref` works. A run that meets a new note or mark warns `Reference 'pdfnotes.N.s' undefined`,
which makes latexmk run again. A third run is needed only when the layout moved.

**Builds without notes.** `\usepackage[disable]{todonotes}` (as a nocomments or submission
build loads it), or `\usepackage[disable]{pdfnotes}` (also `final`), switches everything
off: no todonotes boxes, no annotations, no `/PDFNotes` key, and a mark typesets its text
plainly. The package option passes `disable` on to todonotes when todonotes is not loaded
yet, so plain `\todo`s go too. A class option `final` (IEEEtran and others take one) does
not switch the notes off.

## Interface

Everything below is the contract that `sync_to_source` and other readers rely on.

### Commands

| Command | |
|---|---|
| `\pdfnote[keys]{text}` | A note: a todonotes box and a Text annotation over it |
| `\pdfmark[keys]{marked text}{note}` | A mark over `marked text` carrying `note`; also `\pdfnotemark`, which is always defined (`\pdfmark` is only defined when nothing else defines it: hyperref's dvips driver does) |
| `\newpdfnote{\cmd}{keys}` | Defines `\cmd[keys]{text}` = `\pdfnote[<keys>,keys]{text}`; the keys may also be given as `[keys]`, or left out: `\newpdfnote{\cmd}` |
| `\newpdfmark{\cmd}{keys}` | Defines `\cmd[keys]{marked text}{note}` = `\pdfmark[<keys>,keys]{...}{...}`; keys as for `\newpdfnote` |
| `\pdfnotesetup{keys}` | Defaults for all later notes and marks |

Load the package after todonotes when you load todonotes yourself with options. A key's
value that contains a comma or `=` goes in braces: `author={Smith, J.}`.

### Keys

| Key | Default | Meaning |
|---|---|---|
| `author` | none | `/T` |
| `kind` | none | `/Subj`: todo, comment, done, ... |
| `color` | `orange` | `/C` and the todonotes box: a palette name, `#rrggbb`, or any xcolor colour or expression (rules below) |
| `id` | automatic | The id in `/NM (pdfnotes:<id>)` |
| `replyto` | none | The id of an earlier note or mark: this one is a reply to it (`/IRT`) |
| `prefix` | none | Text in front of the note in the todonotes box (not in `/Contents`), e.g. `{Alice: }` |
| `box` | `true` | Notes: `false` gives no todonotes box, only the annotation at this point of the text (a sticky-note icon; invisible for a reply) |
| `icon` | `none` with a box | Notes: `/Name` of the icon (`Comment`, `Note`, ...); `none` draws nothing over the box |
| `style` | `highlight` | Marks: `highlight`, `underline`, `squiggly`, `strikeout` |
| `textcolor` | none | Marks: the colour of the marked text |
| `note` | `true` | Marks: also show the note in a todonotes box (that box has no annotation of its own) |
| `notecolor` | `color` | Marks: the colour of that box |
| `todo={...}` | | todonotes options; any key not listed here also goes to todonotes |

### What is written

Each note is one Text annotation: its `/Rect` covers the text in the todonotes box (first
line to last, the box's text width) and its appearance is empty, so viewers show the box
and list the note. Each mark is one markup annotation per page with one quad per line
(`/QuadPoints` in the order upper left, upper right, lower left, lower right) and a `/Rect`
around them. Every annotation has `/T`, `/Subj` (when given), `/Contents`, `/NM`, `/C`,
`/F 4` (print), `/M` and `/CreationDate` (the build time), and `/IRT` for a reply.

**Normalisation rules**, so that a writer can compare what it wrote with what it reads back:

- `/T` is exactly the `author` string and `/Subj` exactly the `kind` string, converted as
  `/Contents` is. Without `author` or `kind` the key is left out.
- `/C` is three numbers with at most **4 decimal places** (`0.102 0.1686 0.2353`; `0` and `1`
  without decimals); compare within 1/255.
  - `color=#rrggbb` (either case): `rr/255 gg/255 bb/255`, each rounded to 4 places. The box
    gets the xcolor colour `pdfnotes-RRGGBB`.
  - A palette name, lower case: `blue` 0.6 0.8 1, `yellow` 1 0.9 0.3, `green` 0.6 0.9 0.6,
    `orange` 1 0.7 0.4, `pink` 1 0.7 0.85, `purple` 0.8 0.7 1, `red` 0.9 0.3 0.3, `cyan`
    0.55 0.95 0.95, `grey` and `gray` 0.75 0.75 0.75: the palette of
    `pdf_reader_writer_mcp.pdf.COLORS`, which the viewers use. The box gets the xcolor
    colour `pdfnotes-<name>`; xcolor's own `blue`, `pink` ... are not changed. To get
    xcolor's colour of that name instead, write an expression such as `pink!100`.
  - Anything else is an xcolor colour or expression (`teal`, `red!50!blue`): its RGB as
    xcolor converts it, each component rounded to 4 places.
- `/Contents` is a UTF-16BE string (`<FEFF...>`) with the plain text of the note argument:
  - `\%` `\&` `\#` `\_` `\{` `\}` `\$` give `%` `&` `#` `_` `{` `}` `$`; `\textbackslash{}`,
    `\textasciitilde{}`, `\textasciicircum{}` give `\`, `~`, `^`;
  - `\\` and `\newline` give a line feed (U+000A), and spaces next to a line feed go;
  - a run of spaces (a line end in the source counts as one, as in TeX) is one space, and
    spaces at the start and end go; `~` is a space;
  - non-ASCII characters (UTF-8 input such as `ü`, `é`, `—`) stay the same characters, and
    accent commands such as `\'e` become the character (`é`);
  - markup goes, its text stays: `\emph{x}` and `\textbf{x}` give `x`, `\textcolor{c}{x}`
    gives `x`; `\footnote{...}` goes entirely; other commands go (a braced argument's text
    stays: `\cite{key}` gives `key`);
  - math stays as written, dollars included: `$x^2$` gives `$x^2$`;
  - `prefix` is not part of it.

  So a writer that escapes `\ % & # _ { } $ ~ ^` as above, writes a line feed as `\newline`
  or `\\`, and has no runs of spaces or spaces at the ends gets its text back exactly.

**`/NM (pdfnotes:<id>)`**, on every annotation the package writes:

- `<id>` is the `id` key, verbatim (as TeX reads it, e.g. `id=a_b-1` gives `a_b-1`).
- Without `id`, it is the first 10 hex digits (lower case) of the MD5 of
  `<type>|<author>|<kind>|<marked text>|<note>`, where `<type>` is `note` or `mark`, the
  marked text is empty for a note, and each part is the source as TeX reads it
  (`\detokenize`d: `\emph{x}` is `\emph {x}`). It stays the same as long as that note is
  unchanged, whatever is added before it.
- The second, third ... note or mark with the same id gets `-2`, `-3` ... appended, in
  document order (a repeated explicit `id` also gets a warning).
- All the parts of a mark that runs over a page break carry the same `/NM`, one annotation
  per page (the PDF requires `/NM` to be unique only on its page).
- A printable ASCII `<id>` is written as a literal string (`\`, `(` and `)` escaped), any
  other as UTF-16BE.

**Replies**: `replyto=<id>` writes `/IRT` to the (first) annotation of the earlier note or
mark with that id, so a two-level thread reads back as reply to reply. When no earlier note
or mark has that id (it comes later, or does not exist), the reply is written as a note of
its own, with the warning `replyto=<id>: no earlier note or mark has this id; written as a
note of its own`.

**The document key `/PDFNotes`** in the PDF's Info dictionary:

| Value | When |
|---|---|
| `active` | The notes are on and every note and mark of this run has its annotation |
| `pending` | The notes are on, but at least one note or mark has no annotation in this run: the first run with it (positions not recorded yet), or its page has gone |
| absent | The notes are off (`disable`, `final`, todonotes `disable`), or DVI output |

So a reader can tell whether a build is complete: in a `pending` build, a note or mark of
the sources may be missing from the PDF only because its positions are not known yet.

## How it works

The visible note is todonotes' own `\todo`, unchanged (so its options, `\presetkeys` and
`\listoftodos` behave as before). The annotations are written with the engine's primitives
(pdfTeX `\pdfannot`, LuaTeX `\pdfextension annot`, XeTeX `\special{pdf:ann}`) at shipout,
from positions recorded with zref in the run before:

- a note records where its text starts and ends inside the box;
- a mark records its start and end, the left edge of the column at each (through
  `\vadjust`), and the end of each word that is followed by a space at the top level of the
  marked text (not in braces, brackets or math). Consecutive positions on one baseline make
  a line; the first line starts at the mark, the last ends at its end, the others span the
  column. Lines inside a braced group that breaks over several lines are filled in. These
  positions are whatsits, so the typesetting is the same as without the package.

The package does not use pdfcomment. pdfcomment loads hyperref with its own options and
the obsolete soulutf8. A multi-line `\pdfmarkupcomment` comes out as one annotation per
line, each repeating the note, and needs three runs. It writes CMYK colours for xcolor
names such as yellow. It also has no way to reserve an annotation's object for `/IRT`, or
to sit over a todonotes box.

## Limitations

- Two runs, as above. The annotations come from the build: a reply or an edit made in a
  viewer to one of them is gone after the next build, unless `sync_to_source` carries it
  into the sources first.
- Marks assume justified text: a line that is not the last of the mark is highlighted to
  the right edge of the column, so in ragged-right text it overshoots. A mark cannot
  contain `\verb`. In the marked text, a space between a command and its argument
  (`\foo {x}` after a command that ends in a symbol) becomes a macro; write `\foo{x}`.
- XeLaTeX: a note's annotation is not placed exactly over the todonotes box. pgf moves
  the node with PDF transformations, which XeTeX's recorded positions do not see. Marks
  are exact.
- PyMuPDF (MuPDF) reports a Text annotation's rectangle as its icon's. The `/Rect` in the
  file covers the box.
