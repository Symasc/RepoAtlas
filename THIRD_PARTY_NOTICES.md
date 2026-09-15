# Third-party notices: parser grammars

RepoAtlas parses TypeScript, JavaScript and Bash with tree-sitter grammars that it
**does not vendor and never downloads at run time**. Each grammar is a separate
package dependency, pinned exactly in `pyproject.toml`, built and published by the
tree-sitter organisation, and installed by the package installer. The SHA-256 of
every sdist and every platform wheel is recorded in `uv.lock`, so an installation
from the lockfile verifies the bytes. Each installed distribution carries its own
`LICENSE` in its `.dist-info`.

The same facts are machine-readable in `repoatlas/parsers/grammars.py`, which the
grammar loader uses to refuse any other version or any module the pinned
distribution did not install. The tests hold this file, that table,
`pyproject.toml` and `uv.lock` to one another.

| Grammar(s) | Distribution | Version | Upstream | Tag | Commit | License | sdist SHA-256 |
|---|---|---|---|---|---|---|---|
| `typescript`, `tsx` | `tree-sitter-typescript` | 0.23.2 | https://github.com/tree-sitter/tree-sitter-typescript | v0.23.2 | f975a621f4e7f532fe322e13c4f79495e0a7b2e7 | MIT | 7b167b5827c882261cb7a50dfa0fb567975f9b315e87ed87ad0a0a3aedb3834d |
| `javascript` | `tree-sitter-javascript` | 0.25.0 | https://github.com/tree-sitter/tree-sitter-javascript | v0.25.0 | 44c892e0be055ac465d5eeddae6d3e194424e7de | MIT | 329b5414874f0588a98f1c291f1b28138286617aa907746ffe55adfdcf963f38 |
| `bash` | `tree-sitter-bash` | 0.25.1 | https://github.com/tree-sitter/tree-sitter-bash | v0.25.1 | a06c2e4415e9bc0346c6b86d401879ffb44058f7 | MIT | bfc0bdaa77bc1e86e3c6652e5a6e140c40c0a16b84185c2b63ad7cd809b88f14 |

## Ruby: no grammar is shipped

Ruby files are discovered and indexed for text search, but they are **not parsed
structurally**. The only offline-installable Ruby grammar is `tree-sitter-ruby`
0.23.1, and it predates upstream commit
`ad907a69da0c8a4f7a943a7fe012712208da6dee` ("scanner: fix heredoc serialization
buffer overflows"), a memory-safety fix in the scanner that reads untrusted
source. A Ruby grammar will be adopted only from a release that contains that
commit and installs as a wheel, after the parser's output is re-characterized.

## License texts

### tree-sitter-typescript 0.23.2

```
The MIT License (MIT)

Copyright (c) 2017 Max Brunsfeld

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### tree-sitter-javascript 0.25.0

```
The MIT License (MIT)

Copyright (c) 2014 Max Brunsfeld

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### tree-sitter-bash 0.25.1

```
The MIT License (MIT)

Copyright (c) 2017 Max Brunsfeld

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
