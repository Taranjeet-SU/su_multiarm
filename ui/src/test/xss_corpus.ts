// Synthetic hostile strings (log lines, titles, model replies, URLs). Every view must render them
// inert: as visible text, never as markup, script, event handler or navigable javascript: link.
export const XSS_STRINGS = [
  "<script>window.__xss = 1</script>",
  '<img src=x onerror="window.__xss = 2">',
  "<svg onload=window.__xss=3>",
  '"><iframe src="javascript:window.__xss=4"></iframe>',
  "![pwn](javascript:window.__xss=5)",
  '[click me](javascript:window.__xss=6 "title")',
  "![img](https://attacker.example/x.png)",
  "<a href=\"javascript:window.__xss=7\">link</a>",
  "<style>body{display:none}</style>",
  "\u001b[31mERROR\u001b[0m ansi coloured line \u001b]0;title\u0007",
  "invoice ‮gnp.exe‬ total",
  "<details open ontoggle=window.__xss=8>",
  "{{constructor.constructor('window.__xss=9')()}}",
  "x".repeat(20000),
];

export const XSS_URLS = [
  "javascript:window.__xss=10",
  "JaVaScRiPt:window.__xss=11",
  " javascript:window.__xss=12",
  "java\tscript:window.__xss=13",
  "data:text/html,<script>window.__xss=14</script>",
  "vbscript:msgbox(1)",
  "//attacker.example/path",
  "/relative/path",
];
