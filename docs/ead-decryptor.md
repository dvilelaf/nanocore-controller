# Installing `.ead` files with your own decryptor

The `.ead` amplifier files of Livtra's app and of its tone catalog are encrypted. **This project contains no key and no decryption.** It can still install such a file once you give it a program that decrypts it, for example after the manufacturer has given you the key and the method. Everything else (checking the file, backing up the slot, writing, reading back, keeping the amplifier you were playing) is done here.

## Using it

```bash
export NANOCORE_EAD_DECRYPTOR='python3 /path/to/my-decryptor.py'      # or pass --decryptor on each call
nanocore assets write amp 12 tone.ead                                  # prints the plan (runs your decryptor, touches nothing)
nanocore assets write amp 12 tone.ead --name MyTone --apply --safety-backup backups/amp-12-old.bin
```

The web editor's model panel uses the same program when the server is started with `--ead-decryptor 'PROGRAM ARGS'` (or the environment variable). Without one, an `.ead` is refused with a message that points here.

## The contract of the program

- It is started **without a shell** from the command you give (split like a shell would split it).
- **Standard input**: the `SAPF` container of the file, exactly as the app's code frames it: `SAPF`, version `u32` (1), 16 bytes (a nonce in the files seen), payload size `u32`, the encrypted payload, and a 32-byte authentication code. If the file was wrapped in an `EADL` container, the wrapper is removed first.
- **Standard output**: the decrypted model and nothing else. It must start with `DDPB` and have **exactly the payload size** the container declares (12242 bytes for the amplifiers seen).
- **Exit status** 0 on success. On failure exit with another status and write a short reason to standard error; it is shown to you.
- It has 20 seconds. Anything it prints beyond 64 KiB is refused.

The program should check the authentication code itself and fail if it does not match.

## Where the key goes

Not in this project. Put it where only you can read it and let **your** decryptor read it: the template reads it from the environment variable `NANOCORE_EAD_KEY` (hex) or from the file named by `NANOCORE_EAD_KEY_FILE` (which must be mode 0600). The server and the CLI start the decryptor with your environment, so the variable reaches it.

A key alone is not enough: the decryptor also has to know the **method** (cipher, how the key is turned into the keys that check and decrypt, and what the authentication code covers). Ask the manufacturer for both. The app's code only shows that two keys are derived from a secret with the labels `mac` and `enc`; it does not say how.

**A way to check a decryptor without trusting anyone:** install the same `.ead` with the official app, then read the slot back from the pedal with `nanocore assets read amp N out.bin` (it is stored in the clear). A correct decryptor, given the same `.ead`, must produce exactly those bytes.

## Template

`contrib/ead-decryptor/template.py` is a skeleton with the input and output handling done and the decryption left empty on purpose.

## What this does not do

It does not extract anything from the app, does not contain any scheme or secret, and cannot work until you provide a decryptor. Models that are already in your pedal (installed with the app) never need it: they are stored in the clear and `nanocore assets read`, `backup` and `write` handle them directly.
