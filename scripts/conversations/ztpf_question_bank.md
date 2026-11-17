# ZTPF question bank

Questions a ZTP engineer actually asked the assistant, verbatim (typos kept),
grouped by what the assistant has to be able to do. Source: one engineer's
chat history on a lab box, plus the corrections they left in-chat as
`FIXME:` notes. Use this as the backlog for sweep rows: each covered
question names its row under `sweep/`; everything else is still to author.

Status key: **row** = a sweep row asks it today · **todo** = not covered ·
**blocked** = needs a persona/product change first (see "Gaps" at the end).

## 1. Device status and history (read-only)

| Question | Status |
|---|---|
| what is going on with this device? | row `ztpf_real_whats_going_on` |
| Summarize the latest automation steps for this device, group them by run group and status. | todo (asked 14 times in two wordings) |
| Summarize the current details and steps on this device. | todo (asked 5 times) |
| Explain the current details, status, and latest steps on this device. | todo |
| tell me about this record / Summarize this device. | todo |
| what has trun on thie device recently? / What has recently run on this device? | row `ztpf_real_recent_with_age` |
| What was the result of the last upgrade? Compare the get system status steps. | row `ztpf_real_last_upgrade_result` |
| What steps were run in the last run group? | todo |
| what is the state of steps on this device | todo |
| How many steps have been run on this device? | todo |
| Find all steps related to this device record and simply count them by the data key field. | todo |
| Have we done a ping test from this device? / When have I pinged 8.8.8.8 and what were the results? | todo |
| show me what id 221 did. | todo |
| Show me the config change we are planning to make | todo |
| Why did the last run group fail? | row `ztpf_real_why_failed` |

## 2. Pending steps and run groups

| Question | Status |
|---|---|
| Are there pending steps? / Do we have pending steps? / yes, what's pending? | row `ztpf_real_pending_steps` |
| List this device's automation steps that have no run group yet. | todo |
| Review my pending steps on this device. | todo |
| the steps that are ready are pending / I see 3 steps ready and have no run group | todo (correction turn) |
| Check the run group ztpf-&lt;id&gt; | todo |
| Show current status of the run group | todo |
| Poll the run group to show per-step live progress/status | todo |
| Refresh/poll again to update statuses. | todo (multi-turn) |
| Fetch the full logs/output for any step | todo |
| What interfaces were created in the last run group? / you said it created two interfaces. what were their names? | todo |
| Looks for all the steps that created a new interface and list the interface names. | todo |
| Make me a report that shows the output of all the create interface steps. / yes, re-run the prior report | todo |

## 3. Discovering and running playbooks

| Question | Status |
|---|---|
| What playbooks can I run on this device? (asked 11 times) | row `ztpf_real_playbooks_list` |
| what else can you run for playbooks? / Can you run a playbook from this record? | todo |
| run "Create Run Group and Execute Steps" / Create run group from pending steps / run these steps. | todo |
| Run playbook "Create Run Group and Queue Steps" (old name of a renamed playbook) | row `ztpf_real_run_renamed_playbook` |
| Run playbook "Create Run Group and Queue Steps" even though you cannot find pending steps / run anyway | todo (user overrides the agent's objection) |
| run "Add Steps to Device from Profile" / Run Add Steps rom Profile | todo |
| run "Add Step from Action": | todo |
| run #1 / use record 3 | todo (picks from the agent's own numbered list) |
| Validate this metadata source by running the 'Get Metadata Source Data on Device' playbook and report whether it returned data. | todo (manual-input pause + resume) |

## 4. Adding steps and profiles to a device

| Question | Status |
|---|---|
| Add a step to this device / Add steps to this device | blocked (device persona cannot write steps) |
| Add steps to this device to create a new loopback interface / Add step to create a new test loopback interface | blocked |
| Add steps from a profile that create a new interface | blocked |
| add more steps to this device from a profile / Add a step profile to this device | todo (via the playbook) |
| Yes, add this profile to the device / Add this profile to FG1 | todo |

## 5. Authoring profiles and steps

| Question | Status |
|---|---|
| Help me add a step to this profile. Ask which action to run, read its template's inputs, build the stepARGS, then save it. | row `ztpf_add_step_to_profile` |
| Walk through this profile's steps in order: for each, what action it runs and what stepARGS it passes. | row `ztpf_real_explain_profile`: the live answer stopped at action name + IRI. A step's results are in its RUNS (`ztpf_device_automation_steps`), one hop past the action; the persona routed every action through `ztpfTemplates` (2 of 25 actions have one). Persona fixed, 0/2 -> 2/2 on Frank. |
| Build a profile like an existing one (from the build direction, not the history) | row `ztpf_build_profile_from_similar` |
| Reorder steps in a profile | row `ztpf_reorder_profile_steps`: FAILS on a content defect, not ours. The moves are right (Move Step playbook), but that playbook never rewrites stepARGS position refs (`diff_steps` etc.), and models do not fix them reliably (gpt-5.4 claimed they were fine). Fix belongs in the playbook. |

## 6. Automation actions

| Question | Status |
|---|---|
| Which devices has this automation action run on? / List all the devices that this actoin has been run | row `ztpf_real_action_devices` |
| Use the `Show Interface Config` as an example to run a `get router info routing-table all` on a remote FG. | todo (create similar action) |
| You just need to make an action that can run the command. It will run through FMG when created as a "FortiManager" and "Remote CLI". | todo (correction turn) |
| Create an action from a similar one | row `ztpf_create_action_from_similar` |

## 7. Templates: explain, test, generate, make similar

| Question | Status |
|---|---|
| Explain what this ZTPF template's script provisions and which inputs it consumes, step by step. | row `ztpf_real_explain_template` |
| Use an existing template to find the next loopback but create this script to find the next vlan interface to use. | row `ztpf_author_template_like_loopback_vlan` |
| Update this template script with a jinja script that builds this config | todo (edit + test) |
| Using the jinja_vars from another step, (pasted vars) | todo (render with borrowed context) |
| Use jinja from the data we get in the metadata source linked to this record | todo (follow the link for render context) |
| Help me make a script to read each interface that is of type physical and udate the description | todo (generate) |
| Create Single Loopback Template Test / Yes do Create Single Loopback Template Test | todo |
| Test this template. What config would it push? (persona quick action) | row `ztpf_author_test_template` |

## 8. Metadata sources

| Question | Status |
|---|---|
| Explain what device metadata this source collects and how (which action/command and datakey). | todo |
| Help me create this source. I want it to query the API `monitor/system/available-interfaces` resource from a FG. | row `ztpf_author_metadata_like_this` (test world persona; box persona still blocked) |
| Update the settings of this record to look like record 6 but point to "monitor/system/available-interfaces" | blocked |
| Use one of the other Proxy API records and update this record to get data from "monitor/system/available-interfaces" from a FG. | blocked |
| only update the `datakey` and `script` fields. / You proposed the datakey but I want the script too / now you did not update the datakey | row `ztpf_author_metadata_update_two_fields` (single-turn form) |
| Also update the data key to something that makes sense. / update the script to be pretty printed / ok...did it work? | blocked |
| Set this record up to get all loopback interfaces from a device. Use one of the other records in this module as an example. | blocked |
| The record would need to be create with a name that is called `network database` and then the script would be the raw data to store multiple networks. | blocked |
| Can you change this to a YAML script instead? / I don't want a playbook. I want you to just print me an example yaml. | todo (answer in chat, no build) |

## 9. Module-wide (list page, no record mounted)

| Question | Status |
|---|---|
| Summarize all the devices in this module | todo (needs list-page mount) |
| Yes summarize the last run group of each device and show me when this was run | todo |
| Help me make an API that filters on vlans | todo |

## 10. Odd ones worth keeping

| Question | Status |
|---|---|
| The task_id 2227 is not a FortiSOAR record but a task in the FMG that can be API called. | todo (must not look it up as a record) |
| show me prompts | todo |
| When I say steps I mean ztpf_device_automation_steps | todo (vocabulary correction) |

## The engineer's corrections (FIXMEs)

Each is a behaviour to grade, ideally delivered as a house-rule skill rather
than a prompt change.

1. Don't show playbooks that are disabled or have "deprecated" in the title. (fixed in the tool for disabled; covered by `ztpf_real_playbooks_list`)
2. Include "how long ago" the run groups ran. (skill in `ztpf_world_live`; covered)
3. Run groups newest first, but steps inside a group in step-number order.
4. "Pending" means steps with no run group.
5. "Run the pending steps" means the run-group playbook.
6. Fuzzy-match playbook names when the named one isn't found. (covered by `ztpf_real_run_renamed_playbook`)
7. Action to devices: search steps linked to the action, then collect their devices.
8. No "Build playbook" offer on the actions screen.
9. On a module list page, help create a new record there.
10. Input collected in chat didn't reach the running playbook (manual input bug).
11. After running the run-group playbook, focus on that run group, not older ones.
12. An FMG task id is not a FortiSOAR record.

## Gaps that block rows

- `ztpf_metadata_sources` persona ON THE BOX: `may_write` lacks its own
  module and its tools lack `test_template`, so every create/update ask in
  section 8 fails there. The test world's copy has both plus an AUTHORING
  paragraph (start from the most similar record, exact API path, no made-up
  exampleOutput, test before writing); copy it to the box persona to unblock.
- `ztpf_devices` persona: can't write `ztpf_device_automation_steps`, so
  "add a step to this device" can only go through the playbook.
- No personas for `ztpf_device_automation_steps` or `ztpf_run_groups`.
- No list-page (no record mounted) surface for section 9.
