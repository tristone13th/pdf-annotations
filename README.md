# pdf-annotations
This is a script which extracts PDF annotations to a markdown file. This script is mainly based on [pdfannots](https://github.com/0xabu/pdfannots) and adds some new features:

- **Structured outlines**. This script can output outlines as headings in markdown, which improves clarity.
- **UTF-8 encoding**. Some operating systems (e.g. Windows) use GBK as default encoding, which may cause encoding errors. We set the default encoding of output file as UTF-8 to make it more robust.
- **Formatted output file name**. If the output file name is not specified in command line, it will set as "XXXX-XX-XX-Reading Notes for \<input file name>" by default. This feature is for better integration of blogging systems such as [Jekyll](http://jekyllcn.com/).
- **YAML header**. The output file will contain a YAML header with two keys: "categories" and "title" and automatically set their values. This feature is also for blogging systems.

How to use: Put your pdf file under this directory, then double-click `pdfannots.cmd`.