// Which edition runs: the cloud (SaaS) or CountVision Local (one PC, offline). From GET /api/edition.

export interface Edition {
  edition: "cloud" | "local";
  version: string;
  needs_setup: boolean; // local: no account yet -> the first-start wizard
  email: boolean; // a mail server is set up
  google: boolean;
  signup: boolean; // the public sign-up page exists
  telegram: boolean;
  billing: "stripe" | "off" | "licence";
  live_picture?: boolean; // CountVision Local: the real camera picture is shown
}

export const CLOUD_DEFAULT: Edition = {
  edition: "cloud", version: "", needs_setup: false, email: true, google: false, signup: true,
  telegram: true, billing: "off",
};
