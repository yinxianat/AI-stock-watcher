// "Did you mean …?" for common email-domain typos. A link sent to a misspelled
// domain never arrives and we're never told, so catch it before sending.

const DOMAIN_FIXES = {
  'gmail.com': ['gmial.com', 'gmal.com', 'gmai.com', 'gamil.com', 'gnail.com', 'gmaill.com',
    'gmail.co', 'gmail.con', 'gmail.cm', 'gmail.om', 'gmail.comm'],
  'yahoo.com': ['yaho.com', 'yahooo.com', 'yhoo.com', 'yahoo.co', 'yahoo.con', 'yahoo.cm'],
  'hotmail.com': ['hotmial.com', 'hotmal.com', 'hotmai.com', 'hotmail.co', 'hotmail.con'],
  'outlook.com': ['outlok.com', 'outllook.com', 'outlook.co', 'outlook.con'],
  'icloud.com': ['iclod.com', 'icoud.com', 'icloud.co', 'icloud.con'],
};

const TYPO_TO_DOMAIN = Object.fromEntries(
  Object.entries(DOMAIN_FIXES).flatMap(([good, typos]) => typos.map((t) => [t, good])),
);

// Returns the corrected address, or null if nothing looks wrong.
export function suggestEmail(email) {
  const at = email.lastIndexOf('@');
  if (at < 1) return null;
  const domain = email.slice(at + 1).trim().toLowerCase();
  const fixed = TYPO_TO_DOMAIN[domain];
  return fixed ? `${email.slice(0, at).trim()}@${fixed}` : null;
}
