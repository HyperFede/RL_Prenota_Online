import pytest
from selenium import webdriver


@pytest.fixture(scope="session")
def chrome():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    driver = webdriver.Chrome(options=options)
    yield driver
    driver.quit()


@pytest.fixture
def load_html(chrome, tmp_path):
    def load(html):
        page = tmp_path / "page.html"
        page.write_text(html, encoding="utf-8")
        chrome.get(page.as_uri())
        return chrome
    return load
