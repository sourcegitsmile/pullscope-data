# pullscope-data

Repo chỉ chứa dữ liệu cho app Pullscope (dec_0017). Hằng đêm (21:40 UTC) GitHub Actions dựng lại thẻ + giá, tải thêm ảnh YGO mới rồi commit `v1/` và `img/` vào `main`. GitHub Pages phục vụ `main`, thư mục gốc: https://sourcegitsmile.github.io/pullscope-data/ — đây là `AppConfig.dataBaseURL` của app.

```
.github/workflows/data-buffer.yml   cron → build → mirror ảnh YGO → package → kiểm dung lượng → commit vào main
pipeline/                           build.py · mirror_images.py · package.py (bản gốc ở tcc/spike/cache, chép bằng sync.sh)
check_size.py                       chặn nếu site > 900 MB (Pages giới hạn 1 GB)
v1/  img/                           do workflow ghi, không sửa tay
```

Site:

```
v1/manifest.json
v1/<game>/catalog-<sha>.json.gz
v1/<game>/prices-<day>.json.gz
img/yugioh/<sha16>.webp            chỉ Yu-Gi-Oh! (YGOPRODeck cấm hotlink); 4 game còn lại trỏ thẳng nguồn
```

Chạy tay: Actions → data-buffer → Run workflow (`gh workflow run data-buffer -R sourcegitsmile/pullscope-data`). Không có secret; push bằng `GITHUB_TOKEN`. Ảnh đã đăng được nạp lại vào `pipeline/out/img` trước mỗi lần chạy (tên file = hash URL nguồn), nên chỉ ảnh mới mới phải tải.

## Dung lượng ảnh YGO (đo 2026-11-12)

Toàn bộ catalog YGOPRODeck có 14,836 ảnh (14,664 thẻ). Mẫu đo là 60 ảnh ngẫu nhiên, ảnh gốc 813×1185 JPEG.

| Cách lưu | TB / ảnh | Cả catalog |
|---|---:|---:|
| Gốc | 165 KB | ~2.45 GB |
| JPEG 420 px q60 (Pillow) | 46 KB | ~677 MB |
| **WebP 420 px q65 (đang dùng)** | **37 KB** | **~550 MB** |
| WebP 360 px q65 | 30 KB | ~441 MB |

Chữ trên thẻ vẫn đọc được ở WebP 420 q65 (đã so bằng mắt). Màn chi tiết thẻ hiện ảnh rộng 132 pt, tức 396 px @3x, nên 420 px là đủ. iOS 17 đọc WebP sẵn.

Ảnh được lưu theo URL nguồn, không theo từng bản in. Mỗi ảnh YGO dùng chung cho mọi bản in của thẻ (1,050 bản in mẫu chỉ có 270 ảnh). Cách lưu cũ theo id bản in làm dung lượng tăng khoảng 4 lần, nhiều khả năng là lý do con số ước tính ~1.3 GB trước đây cao.

## Khi vượt giới hạn

`check_size.py` làm build đỏ trước khi Pages từ chối. Có hai hướng:
- Hạ `WIDTH`/`QUALITY` trong `mirror_images.py`. Ví dụ 360 px còn khoảng 441 MB.
- Tách `img/` sang repo thứ hai, `pullscope-img`, cũng dùng Pages. Khi đó chạy `package.py --image-base` với URL repo ảnh.

Tốc độ tăng ước tính: YGO ra khoảng 500 thẻ mới mỗi năm, thêm khoảng 19 MB/năm.
