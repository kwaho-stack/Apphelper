"""PyInstaller/소스 실행 진입점."""
import sys

if __name__ == "__main__":
    if "--cover" in sys.argv:  # 화면 가리기 프로세스 (Qt 를 불러오지 않아 빨리 뜬다)
        from apphelper.cover.guard import main as cover_main
        sys.exit(cover_main())
    from apphelper.app import main
    sys.exit(main())
