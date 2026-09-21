// CAMEO root codes, the 20 event classes GDELT reduces every event to.
export const CAMEO_ROOT: Record<string, string> = {
  "01": "make public statement", "02": "appeal", "03": "express intent to cooperate",
  "04": "consult", "05": "engage in diplomatic cooperation", "06": "engage in material cooperation",
  "07": "provide aid", "08": "yield", "09": "investigate", "10": "demand",
  "11": "disapprove", "12": "reject", "13": "threaten", "14": "protest",
  "15": "exhibit force posture", "16": "reduce relations", "17": "coerce", "18": "assault",
  "19": "fight", "20": "use unconventional mass violence",
};
export const QUAD_CLASS: Record<number, string> = {
  1: "verbal cooperation", 2: "material cooperation", 3: "verbal conflict", 4: "material conflict",
};
