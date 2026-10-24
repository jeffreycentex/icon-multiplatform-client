# Third-party notices

## kappanhang

`icomremote/rsba1/` is a Python port of the RS-BA1 protocol implementation in
[kappanhang](https://github.com/nonoo/kappanhang) (packet layouts, login
sequence, sequence/retransmit handling, passcode table). The tests use a radio
capture published in kappanhang's source.

```
MIT License

Copyright (c) 2020 Norbert Varga HA2NON, Akos Marton ES1AKOS

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

/Icom is a registered trademark of Icom Incorporated (Japan)/
```

The passcode algorithm is credited by kappanhang to W6EL.

## kiwisdr-remote

`icomremote/sinks.py` and `icomremote/resample.py` come from
[kiwisdr-remote](https://github.com/jeffreycentex/kiwisdr-remote)
(GPL-3.0-or-later, same author as this project).
