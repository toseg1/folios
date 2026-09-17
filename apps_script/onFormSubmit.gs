/**
 * folios — instant gross-mismatch flag on BUY/SELL entries.
 * Build-plan step 17. See docs/folios-build-plan.md and PRIVACY.md.
 *
 * This is bound to the FORM itself, not to a response Sheet. folios has
 * no native "linked" response Sheet (the Forms REST API has no way to
 * create one — see forms.py); folios' own response Sheet is populated
 * asynchronously by `folios pull`, so at the instant a response is
 * submitted there is no row yet to edit. Email is the one channel
 * guaranteed to reach Théo within seconds regardless of when the next
 * `folios pull` happens to run.
 *
 * The loader's validation (`folios validate` / `folios load`) remains
 * authoritative — the same quantity x price vs gross check runs there
 * too, on every pull. This script exists purely for speed: catching a
 * mistyped decimal at the moment of entry rather than at the next
 * scheduled sync.
 *
 * --- Install (Théo does this by hand — see build-plan §7 item 6) ---
 * 1. Open the Form in the browser, click the kebab menu (⋮) top right,
 *    then "Script editor".
 * 2. Replace the default Code.gs contents with this file.
 * 3. Left sidebar clock icon -> Triggers -> Add trigger:
 *      function to run:      onFormSubmit
 *      event source:         From form
 *      event type:           On form submit
 *    Save, then authorise the requested permissions when prompted
 *    (this script only ever calls MailApp.sendEmail — see PRIVACY.md).
 * 4. For the edit link in the email to work, turn on Form Settings ->
 *    Responses -> "Allow response editing" (optional — the mismatch
 *    email is still useful without it, just without a one-tap fix link).
 */

var OWNER_EMAIL = Session.getEffectiveUser().getEmail();
var MISMATCH_TOLERANCE = 0.01; // matches build-plan §4's own "within 1 cent"

function onFormSubmit(e) {
  var response = e.response;
  var answers = readAnswers_(response);

  var type = answers['Type'];
  if (type !== 'BUY' && type !== 'SELL') {
    return; // only Trade entries carry quantity, price and gross together
  }

  var quantity = parseDecimal_(answers['Quantity']);
  var price = parseDecimal_(answers['Price']);
  var gross = parseDecimal_(answers['Gross']);
  if (quantity === null || price === null || gross === null) {
    return; // Gross is optional on the Form — nothing to reconcile yet
  }

  var expectedGross = quantity * price;
  if (Math.abs(expectedGross - gross) <= MISMATCH_TOLERANCE) {
    return;
  }

  sendMismatchEmail_(response, type, answers, expectedGross, gross);
}

function readAnswers_(response) {
  var answers = {};
  response.getItemResponses().forEach(function (itemResponse) {
    answers[itemResponse.getItem().getTitle()] = itemResponse.getResponse();
  });
  return answers;
}

function parseDecimal_(value) {
  if (value === undefined || value === null || value === '') {
    return null;
  }
  var n = parseFloat(String(value).replace(',', '.'));
  return isNaN(n) ? null : n;
}

function sendMismatchEmail_(response, type, answers, expectedGross, enteredGross) {
  var editUrl = '';
  try {
    editUrl = response.getEditResponseUrl();
  } catch (err) {
    // "Allow response editing" isn't turned on — the email is still
    // useful without a fix-it link.
  }

  var subject = 'folios: gross mismatch on a ' + type + ' entry';
  var lines = [
    'Symbol:   ' + (answers['Symbol'] || '?'),
    'Quantity: ' + answers['Quantity'],
    'Price:    ' + answers['Price'],
    'Gross entered:  ' + enteredGross.toFixed(2),
    'Quantity x Price: ' + expectedGross.toFixed(2),
    '',
  ];
  if (editUrl) {
    lines.push('Fix it here: ' + editUrl);
  } else {
    lines.push('Submit a correcting entry, or fix it by hand in data/manual/ before the next sync.');
  }

  MailApp.sendEmail(OWNER_EMAIL, subject, lines.join('\n'));
}
