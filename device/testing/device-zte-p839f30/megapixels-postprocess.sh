#!/bin/sh
# Megapixels postprocessor for the ZTE Blade S6.

set -u

if [ "${1:-}" = "--dry-run" ]; then
	DRY_RUN=1
	shift
else
	DRY_RUN=0
fi
if [ "$#" -ne 3 ]; then
	echo "Usage: $0 [--dry-run] BURST_DIR TARGET_NAME SAVE_DNG" >&2
	exit 2
fi

BURST_DIR="$1"
TARGET_NAME="$2"
SAVE_DNG="$3"

printf 'Source: %s/1.dng or 1.jpg\nTarget: %s\nWrite: photo output; remove temporary burst\nChecks: free space, raw conversion, JPEG encoding\n' \
	"$BURST_DIR" "$TARGET_NAME" >&2
[ "$DRY_RUN" -eq 1 ] && exit 0

export OMP_NUM_THREADS=2
export MAGICK_THREAD_LIMIT=2

MAIN_PICTURE="$BURST_DIR/1"

cleanup() {
	rm -rf "$BURST_DIR"
}
trap cleanup EXIT INT TERM

NEEDED_KB=131072
free_kb() {
	df -Pk "$1" 2>/dev/null | awk 'NR==2 {print $4}'
}
for dir in "$(dirname "$TARGET_NAME")" "$BURST_DIR"; do
	avail=$(free_kb "$dir")
	[ -z "$avail" ] && continue
	if [ "$avail" -lt "$NEEDED_KB" ]; then
		echo "postprocess: only ${avail}KiB free on $dir, need ${NEEDED_KB}KiB" >&2
		exit 1
	fi
done

if [ -f "$MAIN_PICTURE.jpg" ]; then
	cp "$MAIN_PICTURE.jpg" "$TARGET_NAME.jpg" || exit 1
	echo "$TARGET_NAME.jpg"
	exit 0
fi

cp "$MAIN_PICTURE.dng" "$TARGET_NAME.dng" || exit 1

DCRAW=""
TIFF_EXT="dng.tiff"
if command -v dcraw_emu > /dev/null; then
	DCRAW=dcraw_emu
elif [ -x /usr/lib/libraw/dcraw_emu ]; then
	DCRAW=/usr/lib/libraw/dcraw_emu
elif command -v dcraw > /dev/null; then
	DCRAW=dcraw
	TIFF_EXT="tiff"
fi

if [ -z "$DCRAW" ]; then
	echo "$TARGET_NAME.dng"
	exit 0
fi

if [ "$DCRAW" = "dcraw" ]; then
	set -- -w
else
	set -- -fbdd 1 -q 1
fi

# +M  embedded colour matrix   -H 4  rebuild highlights   -o 1  sRGB   -T  TIFF
if ! $DCRAW +M -H 4 -o 1 -T "$@" "$MAIN_PICTURE.dng"; then
	echo "postprocess: $DCRAW failed" >&2
	exit 1
fi

CONVERT=""
if command -v magick > /dev/null; then
	CONVERT="magick"
elif command -v convert > /dev/null; then
	CONVERT="convert"
elif command -v gm > /dev/null; then
	CONVERT="gm convert"
fi

if [ -z "$CONVERT" ]; then
	cp "$MAIN_PICTURE.$TIFF_EXT" "$TARGET_NAME.tiff" || exit 1
	echo "$TARGET_NAME.tiff"
	exit 0
fi

if ! $CONVERT "$MAIN_PICTURE.$TIFF_EXT" \
	-sharpen 0x1.0 -sigmoidal-contrast 6,50% \
	-quality 88 -sampling-factor 4:2:0 -strip \
	"$TARGET_NAME.jpg"; then
	echo "postprocess: $CONVERT failed" >&2
	exit 1
fi

# Restore TIFF metadata after JPEG encoding.
if command -v exiftool > /dev/null; then
	exiftool -tagsFromFile "$MAIN_PICTURE.$TIFF_EXT" \
		-software="Megapixels" \
		-overwrite_original "$TARGET_NAME.jpg" > /dev/null 2>&1
fi

echo "$TARGET_NAME.jpg"

if [ "$SAVE_DNG" -eq 0 ]; then
	rm -f "$TARGET_NAME.dng"
fi
