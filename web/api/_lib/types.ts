export type MarketInput = {
  name: string;
  label?: string;
  type?: string;
  required?: boolean;
  description?: string;
};

export type MarketListing = {
  id: string;
  name: string;
  description: string;
  type: "pomerado" | "workflow-use" | "from-task";
  tier: string;
  inputs: MarketInput[];
  version: string;
  created_at: string;
  updated_at: string;
  sha256: string;
  contains_code: boolean;
  size: number;
};

export const LISTING_TYPES = new Set(["pomerado", "workflow-use", "from-task"]);
