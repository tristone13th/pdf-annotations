# pdf-annotations
This is a script which extracts PDF annotations to a markdown file. This script is mainly based on [pdfannots](https://github.com/0xabu/pdfannots) and adds some new features:

- **Structured outlines**. This script can output outlines as headings in markdown, which improves clarity.
- **UTF-8 enconding**. Some operating systems (e.g. Windows) use GBK as default encoding, which may cause encoding errors. We set the default encoding of output file as UTF-8 to make it more robust.