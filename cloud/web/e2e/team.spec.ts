import { expect, test } from "@playwright/test";

import { createOrg, linkFromMail, PASSWORD, signUp, uniqueEmail } from "./helpers";

test("invite a new person, they join, owner changes role and sees activity", async ({ browser, request }) => {
  const ownerEmail = uniqueEmail("boss");
  const staffEmail = uniqueEmail("staff");
  const owner = await (await browser.newContext()).newPage();
  await signUp(owner, "Boss", ownerEmail);
  await createOrg(owner, "Bäckerei Ost");

  await owner.getByRole("link", { name: "Members" }).first().click();
  await owner.getByRole("button", { name: "Invite someone" }).click();
  await owner.getByLabel("Email address").fill(staffEmail);
  await owner.getByLabel("Role").selectOption("viewer");
  await owner.getByRole("button", { name: "Send invitation" }).click();
  await expect(owner.getByText(`Invitation sent to ${staffEmail}.`)).toBeVisible();
  await expect(owner.getByTestId("invite-row")).toContainText(staffEmail);

  // The invited person opens the link in another browser and signs up.
  const staff = await (await browser.newContext()).newPage();
  await staff.goto(await linkFromMail(request, staffEmail, "invite"));
  await expect(staff.getByRole("heading", { name: "Join Bäckerei Ost" })).toBeVisible();
  await expect(staff.getByLabel("Email")).toHaveValue(staffEmail);
  await staff.getByLabel("Your name").fill("Sami Staff");
  await staff.getByLabel("Password").fill(PASSWORD);
  await staff.getByRole("button", { name: "Create account" }).click();
  await expect(staff.getByRole("heading", { name: "Bäckerei Ost" })).toBeVisible();
  await expect(staff.getByText("Please confirm your email address")).toHaveCount(0); // link proved the email

  // Viewer: no invite button, no settings.
  await staff.getByRole("link", { name: "Members" }).first().click();
  await expect(staff.getByRole("button", { name: "Invite someone" })).toHaveCount(0);
  await staff.getByRole("link", { name: "Settings" }).first().click();
  await expect(staff.getByText("Only admins and owners can change settings.")).toBeVisible();

  // Owner promotes the viewer to member.
  await owner.reload();
  await expect(owner.getByTestId("invite-row")).toHaveCount(0);
  await owner.getByLabel("Role of Sami Staff").selectOption("member");
  await expect(owner.getByText("Sami Staff is now member.")).toBeVisible();

  await owner.getByRole("link", { name: "Settings" }).first().click();
  const activity = owner.getByTestId("audit-row");
  await expect(activity.first()).toContainText(`changed the role of ${staffEmail} from viewer to member`);
  await expect(owner.getByText("Sami Staff joined as viewer")).toBeVisible();

  // Owner cannot leave as the only owner.
  await owner.getByRole("link", { name: "Members" }).first().click();
  owner.once("dialog", (d) => d.accept());
  await owner.getByRole("button", { name: "Leave Bäckerei Ost" }).click();
  await expect(owner.getByRole("alert").filter({ hasText: "only owner" })).toBeVisible();
});

test("an invite for someone else is refused, rename and delete organization", async ({ browser, request }) => {
  const ownerEmail = uniqueEmail("own2");
  const invited = uniqueEmail("invited");
  const owner = await (await browser.newContext()).newPage();
  await signUp(owner, "Owner Two", ownerEmail);
  await createOrg(owner, "Imbiss");
  await owner.getByRole("link", { name: "Members" }).first().click();
  await owner.getByRole("button", { name: "Invite someone" }).click();
  await owner.getByLabel("Email address").fill(invited);
  await owner.getByRole("button", { name: "Send invitation" }).click();
  await expect(owner.getByTestId("invite-row")).toHaveCount(1);
  const link = await linkFromMail(request, invited, "invite");

  // The owner (wrong account) opens the link.
  await owner.goto(link);
  await expect(owner.getByText(`You are logged in as ${ownerEmail}`)).toBeVisible();
  await owner.goBack();

  await owner.goto("/app");
  await owner.getByRole("link", { name: "Settings" }).first().click();
  await owner.getByLabel("Organization name").fill("Imbiss am Markt");
  await owner.getByRole("button", { name: "Save name" }).click();
  await expect(owner.getByText("Name saved.")).toBeVisible();

  await owner.getByRole("button", { name: "Delete organization" }).click();
  const confirm = owner.getByRole("button", { name: "Delete forever" });
  await expect(confirm).toBeDisabled();
  await owner.getByLabel('Type "Imbiss am Markt" to confirm').fill("Imbiss am Markt");
  await confirm.click();
  await expect(owner.getByRole("heading", { name: "Name your business" })).toBeVisible();
});
