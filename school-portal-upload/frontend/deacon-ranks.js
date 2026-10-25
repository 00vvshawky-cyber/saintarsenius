/* Single source of truth for deacon-rank display labels.
   Backend stores the CODE (none/epsaltos/...); frontend maps code -> Arabic label here.
   Keep this file in sync with backend/utils/deacon.py DEACON_RANKS. */
window.RANK_LABELS = {
  none: 'لم أرسم بعد',
  epsaltos: 'الإبصالتس',
  anagnostis: 'الأغنسطس',
  epidiakon: 'الإيبودياكون',
  diakon: 'الدياكون',
  archdiakon: 'الأرشيدياكون',
};
// Backwards-compatible aliases used by various pages.
window.RANK_SHORT = window.RANK_LABELS;
window.DEACON_LABELS = window.RANK_LABELS;
